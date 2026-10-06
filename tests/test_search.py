"""Search refreshes its cache incrementally and ranks exact, all-term and any-term matches."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.types import CallToolResult
from pydantic import ValidationError

from bf import index, pages, records, retrieve, storage, usage
from bf.cli import main
from bf.config import register, user_path
from bf.markdown import LEAD, entry_note
from bf.mcp import server
from bf.models import Config, Error, NotFoundError, Query, Record, digest, encode, timestamp
from bf.pages import scope
from bf.retrieve import read, search
from bf.storage import UNNAMED, BusyError, Store, state_store, writer
from bf.update import update
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


def test_function_words_written_as_acronyms_stay_terms(brain: Store) -> None:
    brain.write("concepts/act.md", b"# Act\n\nThe theatre act lasts an hour.\n")
    brain.write("concepts/ai-act.md", b"# Regulation\n\nThe EU AI Act classifies systems by risk.\n")
    # Before 17 the French function words eu and ai dropped, so this query searched only for act.
    assert index.terms("EU AI Act") == ["EU", "AI", "Act"]
    assert refs(brain, "EU AI Act")[0] == "concepts/ai-act.md"
    # Lowercase function words and the habitual AND and OR operators still drop; one letter is no acronym.
    assert index.terms("ai and the IT budget AND I") == ["IT", "budget"]
    assert index.terms("launch OR budget") == ["launch", "budget"]


def test_quoted_words_and_english_subjects_stay_terms(brain: Store) -> None:
    brain.write(
        "concepts/comments.md",
        b"# Comment guidelines\n\nEvery comment on a pull request names the line. A review comment is short.\n",
    )
    brain.write("concepts/review.md", b"# Review checklist\n\nCheck tests and docs before approving a review.\n")
    # Before, comment dropped as a French function word: the first query found nothing and the second ranked the
    # checklist first.
    assert refs(brain, "comment etiquette") == ["concepts/comments.md"]
    assert refs(brain, "review comment") == ["concepts/comments.md", "concepts/review.md"]
    # Quoting keeps a function word as a term, as a phrase keeps its own: French "son" is also an English word.
    brain.write("concepts/family.md", b"# Family\n\nTheir son packs for the trip.\n")
    assert "concepts/family.md" not in refs(brain, "son checklist")
    assert "concepts/family.md" in refs(brain, '"son" checklist')
    # Before, a quoted acronym dropped too, although the same word unquoted stays a term.
    assert index.terms('"the" review "EU" act') == ["the", "review", "EU", "act"]


def test_results_are_compact_and_cite_readable_refs(brain: Store) -> None:
    reply = search([brain], Query(text="durable evidence"))
    items = reply["items"]
    assert isinstance(items, list)
    record = next(i for i in items if i["kind"] == "record")
    # With one selected brain, items name neither it nor their address; a record's kind is its only type.
    assert record == {
        "excerpt": "The team chose offline retrieval.",
        "kind": "record",
        "ref": "meetings:decision-1",
        "source": "meetings",
        "time": "2026-08-31T12:00:00.000000Z",
        "title": "Preserve durable evidence",
        # Searches retain collected excerpts; the reply's notice marks all retrieved content as untrusted.
    }
    assert "untrusted" in str(reply["notice"])
    # Note excerpts preview the section's words on one line; its heading is already the title.
    note = cast("list[dict[str, str]]", search([brain], Query(text="historical evidence"))["items"])[0]
    assert (note["ref"], note["title"]) == ("projects/offline.md#decision", "Offline retrieval — Decision")
    assert note["excerpt"].startswith("Provider retention cannot guarantee")
    concepts = search([brain], Query(text="durable evidence", **scope("concepts")))["items"]
    assert cast("list[dict[str, str]]", concepts)[0]["excerpt"] == "Originals outlive providers."


def test_long_titles_are_previews_in_results_and_exact_in_reads(brain: Store) -> None:
    # Some sensors title a record with a whole message, such as a commit body.
    title = "Fix the release gate " + " ".join(f"step{n}" for n in range(300))
    records_file(brain, "commits", [Record(id="c1", title=title, text="release gate")])
    item = cast("list[dict[str, str]]", search([brain], Query(text="release gate"))["items"])[0]
    assert item["ref"] == "commits:c1"
    assert len(item["title"]) <= index.TITLE
    # Cut at a word boundary, without a dangling space before the ellipsis.
    assert item["title"].endswith("…")
    assert not item["title"].endswith(" …")
    assert title.startswith(item["title"].removesuffix("…"))
    assert cast("dict[str, str]", read([brain], "commits:c1")["record"])["title"] == title
    # Short titles are unchanged, and a single long word is cut at the limit.
    word = "x" * 500
    records_file(brain, "commits", [Record(id="c2", title=word, text="lonely word")])
    item = cast("list[dict[str, str]]", search([brain], Query(text="lonely word"))["items"])[0]
    assert item["title"] == "x" * (index.TITLE - 1) + "…"


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
    # The undated page is a scope too; any other segment below a source scopes nothing, so it is refused.
    records_file(brain, "meetings", [Record(id="minutes", title="Lunch minutes")])
    assert refs(brain, "lunch", **scope("memories/meetings/undated")) == ["meetings:minutes"]
    for unknown in ("memories/meetings/anything", "memories/meetings/2026-08-30/x", "memories/meetings/x.json"):
        with pytest.raises(Error, match="scope accepts"):
            scope(unknown)
    # An identity scope covers its owning note and the items that link to it.
    assert refs(brain, "offline", **scope("repo:example/project")) == ["projects/offline.md", "meetings:decision-1"]
    assert refs(brain, "offline", **scope("meetings:decision-1")) == ["projects/offline.md", "meetings:decision-1"]
    assert refs(brain, "evidence", limit=1) == ["concepts/evidence.md"]
    for invalid in ["bf.yaml", "sensors", "../x", "memories/../bf.yaml", "soon"]:
        with pytest.raises(Error, match="scope accepts"):
            scope(invalid)
    assert scope("") == {}


def test_demoted_notes_rank_last_but_stay_visible(brain: Store) -> None:
    brain.write(
        "projects/old.md",
        b"---\ntype: project\nstatus: deprecated\n---\n# Old offline retrieval\n\nOffline retrieval.\n",
    )
    # Only deprecated closes a note; other words, as in an attachment's frontmatter, rank normally.
    brain.write("projects/wip.md", b"---\nstatus: wip\n---\n# Unfinished offline retrieval\n\nOffline retrieval.\n")
    found = refs(brain, "offline retrieval")
    assert found.index("projects/old.md") > found.index("meetings:decision-1")
    assert found.index("projects/wip.md") < found.index("meetings:decision-1")


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
    brain.write("projects/broken.md", b"---\nstale_after: soon\n---\n# Broken\n")
    brain.write("memories/bad/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json", b'{"id":"x"}\n')
    records_file(brain, "dupes", [Record(id="a", title="A", time="2026-09-01T00:00:00Z")])
    brain.write("memories/dupes/" + "0" * 64 + ".json", b'{"id":"a","title":"A"}\n')
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    problems = cast("list[dict[str, str]]", index.status(brain)["problems"])
    assert len(problems) == 3
    # Each skipped file is one object naming it once.
    assert {"file": "projects/broken.md", "error": problems[2]["error"]} == problems[2]
    assert problems[2]["error"].startswith("invalid frontmatter: stale_after")
    assert any("record id does not match" in p["error"] for p in problems)
    reply = search([brain], Query(text="offline"))
    assert {"brain": "fixture", **problems[2]} in cast("list[dict[str, str]]", reply["problems"])


def test_unaddressable_names_are_reported_without_hiding_the_brain(brain: Store) -> None:
    # A Latin-1 name from an old archive, or a backslash, cannot be a ref: report it and keep answering.
    brain.write("actions/2026-09-26_demo/ACTION.md", b"---\ntype: action\n---\n# Demo\n\nResume the offline import.\n")
    created = []
    for name in (
        b"concepts/caf\xe9.md",
        b"actions/2026-09-26_demo/inputs/data\xe9.csv",
        b"memories/meetings/caf\xe9.json",
        b"concepts/back\\slash.md",
    ):
        target = brain.root / os.fsdecode(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(b"# Unaddressable offline note\n")
        except OSError:
            continue  # APFS stores only UTF-8 names
        created.append(name.decode("utf-8", "backslashreplace"))
    assert index.refresh(brain, full=True)["skipped"] == len(created)
    reply = search([brain], Query(text="offline"))
    assert cast(list, reply["items"])[0]["ref"] == "projects/offline.md"
    problems = cast("list[dict[str, str]]", reply["problems"])
    assert {problem["file"]: problem["error"] for problem in problems} == dict.fromkeys(created, UNNAMED)
    action = read([brain], "actions/2026-09-26_demo")
    assert action["files"] == []
    attachment = [name for name in created if name.startswith("actions/")]
    assert [p["file"] for p in cast(list, action["problems"]) if p.get("file", "").startswith("actions/")] == attachment
    report = validate(brain)
    assert {p["file"]: p["error"] for p in cast(list, report["problems"])} == dict.fromkeys(created, UNNAMED)
    # Collection never binds such a name: listing the source fails closed and names it.
    if any(name.startswith("memories/") for name in created):
        with pytest.raises(Error, match=r"memories/meetings/caf\\xe9\.json: file name is not valid UTF-8"):
            brain.files("memories/meetings")


def test_editor_locks_beside_notes_never_fail_retrieval_or_updates(brain: Store) -> None:
    # Emacs keeps a dangling `.#NAME` link, or a small file, beside a note while its buffer has unsaved edits.
    brain.write("actions/2026-09-26_demo/ACTION.md", b"---\ntype: action\n---\n# Demo\n\nResume the offline import.\n")
    (brain.root / "projects/.#offline.md").symlink_to("owner@host.12345:1790000000")
    (brain.root / "actions/2026-09-26_demo/.#ACTION.md").symlink_to("owner@host.12345:1790000000")
    brain.write("concepts/.#evidence.md", b"owner@host.12345:1790000000")
    assert index.refresh(brain, full=True)["skipped"] == 0
    assert "problems" not in search([brain], Query(text="offline"))
    assert validate(brain)["valid"]
    action = read([brain], "actions/2026-09-26_demo")
    assert (action["files"], "problems" in action) == ([], False)
    assert update(brain)["ok"]
    # Other links in note folders are still reported.
    (brain.root / "projects/linked.md").symlink_to(brain.root / "bf.yaml")
    assert index.refresh(brain)["skipped"] == 1


def test_foreign_frontmatter_keeps_ordinary_markdown_searchable(brain: Store) -> None:
    # A document copied from another tool may carry frontmatter that is not valid YAML; OKF notes stay strict.
    brain.write(
        "actions/2026-09-26_vendor/ACTION.md",
        b"---\ntype: action\n---\n# Vendor review\n\n[Notes](inputs/vendor.md) and [draft](inputs/draft.md)\n",
    )
    brain.write(
        "actions/2026-09-26_vendor/inputs/vendor.md", b"---\ntitle: Release: notes\n---\n# Vendor\n\nZircon steps.\n"
    )
    brain.write("actions/2026-09-26_vendor/inputs/draft.md", b"---\nZircon draft without a closing rule\n")
    assert index.refresh(brain, full=True)["skipped"] == 0
    assert sorted(refs(brain, "zircon")) == [
        "actions/2026-09-26_vendor/inputs/draft.md",
        "actions/2026-09-26_vendor/inputs/vendor.md",
    ]
    assert str(read([brain], "actions/2026-09-26_vendor/inputs/vendor.md")["text"]).startswith("---\ntitle: Release")
    assert validate(brain)["valid"]
    brain.write("projects/strict.md", b"---\ntitle: Release: notes\n---\n# Strict\n")
    assert index.refresh(brain)["skipped"] == 1
    assert [p["file"] for p in cast(list, validate(brain)["problems"])] == ["projects/strict.md"]


def test_links_and_special_files_are_reported_without_hiding_the_brain(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nExfiltrated secret.\n")
    brain.write("actions/2026-09-26_demo/ACTION.md", b"---\ntype: action\n---\n# Demo\n\nResume the offline import.\n")
    # Every skipped link is reported: even a .bin path could point to a directory of notes.
    (brain.root / "actions/2026-09-26_demo/inputs").mkdir()
    (brain.root / "actions/2026-09-26_demo/inputs/dataset.bin").symlink_to(outside)
    (brain.root / "concepts/linked.md").symlink_to(outside)
    os.mkfifo(brain.root / "memories/meetings/pipe.json")
    reply = search([brain], Query(text="offline"))
    assert cast(list, reply["items"])[0]["ref"] == "projects/offline.md"
    problems = cast("list[dict[str, str]]", reply["problems"])
    assert [problem["file"] for problem in problems] == [
        "actions/2026-09-26_demo/inputs/dataset.bin",
        "concepts/linked.md",
        "memories/meetings/pipe.json",
    ]
    assert "symlinks are not followed" in problems[0]["error"]
    assert refs(brain, "exfiltrated") == []
    assert index.status(brain)["cache"] == "ready"
    assert "projects" in read([brain])
    action = read([brain], "actions/2026-09-26_demo")
    assert "actions/2026-09-26_demo/inputs/dataset.bin" not in cast(list, action["files"])
    assert {
        "brain": "fixture",
        "file": "actions/2026-09-26_demo/inputs/dataset.bin",
        "error": "symlinks and special files are not listed",
    } in cast(list, action["problems"])
    # The cache names the record file, and a skipped file keeps an absent record from looking absent.
    assert cast(dict, read([brain], "meetings:decision-1")["record"])["id"] == "decision-1"
    with pytest.raises(Error, match="unreadable records"):
        read([brain], "meetings:missing")
    report = validate(brain)
    assert not report["valid"]
    assert sorted(p["file"] for p in cast(list, report["problems"])) == [
        "actions/2026-09-26_demo/inputs/dataset.bin",
        "concepts/linked.md",
        "memories/meetings/pipe.json",
    ]


def test_search_says_when_results_exist_beyond_the_limit(brain: Store) -> None:
    assert search([brain], Query(text="offline", limit=1))["next_offset"] == 1
    reply = search([brain], Query(text="offline", limit=50))
    assert "next_offset" not in reply
    assert "more" not in reply


def test_full_build_creates_every_index_after_loading_and_serves_in_wal_mode(brain: Store) -> None:
    index.refresh(brain, full=True)
    brain.write("concepts/late.md", b"# Late\n\nOsmium evidence.\n")
    assert refs(brain, "osmium") == ["concepts/late.md"]
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        created = {row[0] for row in connection.execute("SELECT sql FROM sqlite_schema WHERE type='index'") if row[0]}
    assert created == set(index._INDEXES)  # noqa: SLF001 - deferred secondary indexes


def test_replies_find_skipped_files_through_their_own_index(brain: Store) -> None:
    brain.write("projects/broken.md", b"---\nstale_after: soon\n---\n# Broken\n")
    index.refresh(brain, full=True)
    statements: list[str] = []
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(statements.append)
        assert [problem["file"] for problem in index.problems(connection)] == ["projects/broken.md"]
        assert retrieve._complete(connection, "ready", "meetings")  # noqa: SLF001 - an exact record read's check
        connection.set_trace_callback(None)
        # Every reply checks for skipped files: only their rows are read, not the row of every file.
        assert len(statements) == 2
        for statement in statements:
            plan = " ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
            assert "files_problems" in plan, plan


def test_one_reply_compares_files_with_the_cache_once_per_brain(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[Path] = []
    original = index.fresh

    def counted(store: Store, counts: dict[str, int] | None = None) -> str:
        checked.append(store.root)
        return original(store, counts)

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


def plant(store: Store, *statements: str) -> None:
    """Add rows no brain file supports, as a cloned or extracted cache could carry, and optional schema objects."""
    with closing(sqlite3.connect(store.root / index.CACHE)) as connection, connection:
        connection.execute(
            "INSERT INTO items(id,ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial,"
            "tasks_open,tasks_done,next,weight,stale_after,fields) VALUES(999,'projects/roadmap.md',"
            "'projects/roadmap.md','note','','Roadmap','','project','','planted','','','',0,0,0,'',1,'','')"
        )
        connection.execute("INSERT INTO passages(id,item,fragment,title,original) VALUES(999,999,'','Roadmap','')")
        connection.execute("INSERT INTO search(rowid,title,text,names) VALUES(999,'roadmap','zanzibar planted','')")
        for statement in statements:
            connection.execute(statement)


def cache_file(store: Store) -> str:
    with closing(sqlite3.connect(store.root / index.CACHE)) as connection:
        return connection.execute("SELECT file FROM ontology").fetchone()[0]


def test_a_cache_copied_with_its_brain_is_rebuilt_instead_of_trusted(brain: Store, tmp_path: Path) -> None:
    # A copy, clone or archive can carry a cache whose rows no brain file supports; only its file identity differs.
    refs(brain, "offline")
    plant(brain)
    copy = Store(shutil.copytree(brain.root, tmp_path / "copy", symlinks=True))
    assert refs(copy, "zanzibar") == []
    assert refs(copy, "offline")[0] == "projects/offline.md"
    assert cache_file(copy) != cache_file(brain)


@pytest.mark.parametrize(
    "statement",
    ["CREATE TRIGGER planted AFTER INSERT ON files BEGIN SELECT 1; END", "CREATE VIEW planted AS SELECT 1"],
    ids=["trigger", "view"],
)
def test_a_cache_holding_triggers_or_views_is_rebuilt_in_place(brain: Store, statement: str) -> None:
    # bf never creates them: a trigger could add rows on refresh that no brain file supports.
    refs(brain, "offline")
    plant(brain, statement)
    assert refs(brain, "zanzibar") == []
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        found = connection.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()
    assert found == (0,)


def test_a_remount_that_changes_the_device_number_keeps_the_cache(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # btrfs subvolumes, NFS and FUSE mounts can get another device number after a remount or reboot.
    refs(brain, "offline")
    cache, lstat = brain.root / index.CACHE, Path.lstat

    def remounted(self: Path) -> os.stat_result:
        info = lstat(self)
        if self != cache:
            return info
        values = list(info)
        values[2] += 1  # st_dev
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", remounted)
    assert index.refresh(brain)["changed"] == 0


def test_a_busy_writer_serves_the_current_cache_as_stale(brain: Store) -> None:
    refs(brain, "offline")
    brain.write("concepts/late.md", b"# Late\n\nLatecomer.\n")
    with writer(brain):
        reply = search([brain], Query(text="latecomer"))
    # The stale cache does not hold the word yet: `stale` marks that answer, and its unmatched word, as outdated.
    assert reply == {
        "items": [],
        "notice": reply["notice"],
        "stale": ["fixture"],
        "sources": reply["sources"],
        "unmatched": ["latecomer"],
    }
    assert cast(list[dict], reply["sources"])[0]["source"] == "meetings"
    assert refs(brain, "latecomer") == ["concepts/late.md"]


def test_a_missing_cache_behind_a_busy_writer_fails_after_one_wait(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        (brain.root / (index.CACHE + suffix)).unlink(missing_ok=True)
    waits: list[float] = []

    def busy(_store: Store, *, wait: float = 30, **_options: object) -> dict[str, object]:
        waits.append(wait)
        raise BusyError("another writer holds the brain")

    monkeypatch.setattr(index, "refresh", busy)
    # With nothing to serve, the read waits once for the writer, then names it instead of retrying the wait.
    with pytest.raises(BusyError, match="another writer is building the search cache"), index.database(brain):
        pass
    assert waits == [120]


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

    def paused(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        ingest_started.set()
        assert release_ingest.wait(10)
        return original_index(connection, store, path, config)

    def observe_refresh(store: Store, *, wait: float = 30, **options: Any) -> dict[str, object]:
        waits.append(wait)
        reader_refreshing.set()
        return original_refresh(store, wait=wait, **options)

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


def test_failed_or_abandoned_full_rebuilds_keep_the_live_cache(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    index.refresh(brain)
    live = cache_file(brain)
    original_index = index._index  # noqa: SLF001 - inject failure after a file was indexed
    indexed: list[str] = []

    def fail_second(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        if indexed:
            raise sqlite3.OperationalError("simulated disk full")
        indexed.append(path)
        return original_index(connection, store, path, config)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_index", fail_second)
        with pytest.raises(Error, match="check free space"):
            index.refresh(brain, full=True)
    assert len(indexed) == 1
    assert not (brain.root / index.BUILD).exists()
    assert cache_file(brain) == live
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == index.SCHEMA
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 4
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    # A killed build leaves its partial file and journal beside the cache; the next build replaces them. A read
    # would remove them first (tests/test_cache.py), so the build runs next.
    for suffix in ("", "-journal"):
        (brain.root / (index.BUILD + suffix)).write_bytes(b"abandoned build")
    assert index.refresh(brain, full=True)["files"] == 4
    assert sorted(path.name for path in (brain.root / ".bf").iterdir()) == ["index.sqlite"]
    assert cache_file(brain) != live


def test_a_full_build_keeps_serving_and_refreshing_the_live_cache_until_it_publishes(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    live = cache_file(brain)
    ingesting, finish = Event(), Event()
    original_index = index._index  # noqa: SLF001 - pause the build after its scan

    def paused(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        if not ingesting.is_set():
            ingesting.set()
            assert finish.wait(10)
        return original_index(connection, store, path, config)

    monkeypatch.setattr(index, "_index", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        build = executor.submit(index.refresh, brain, full=True)
        try:
            assert ingesting.wait(10)
            # The build holds no writer lock: a collection commits while searches read and refresh the live cache.
            with writer(brain):
                brain.write("concepts/late.md", b"# Late\n\nOsmium evidence.\n")
                brain.delete(records.path("meetings", "lunch"))
            assert refs(brain, "osmium") == ["concepts/late.md"]
            assert cache_file(brain) == live
        finally:
            finish.set()
        # The build fingerprinted its files before the commit: the removed record is neither indexed nor skipped.
        assert build.result(timeout=10) == {"files": 4, "changed": 4, "removed": 0, "skipped": 0}
    assert cache_file(brain) != live
    # The file added after the build's scan has no fingerprint in the new generation: the next refresh indexes it.
    reply = search([brain], Query(text="osmium lunch"), counted=False)
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])] == ["concepts/late.md"]
    assert "problems" not in reply


def test_a_build_publishes_after_readers_close_the_generation_it_replaces(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    waiting = Event()

    def sleep(seconds: float) -> None:
        waiting.set()
        time.sleep(seconds)

    # The publish waits in the generation lock that readers hold while connected.
    monkeypatch.setattr(storage, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with index.database(brain) as (connection, _state):
            build = executor.submit(index.refresh, brain, full=True)
            # SQLite finds -wal and -shm files by name: no reader of the old generation may outlive the rename.
            assert waiting.wait(10)
            assert not build.done()
            assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 4
        assert build.result(timeout=10)["files"] == 4
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    # A reader that outlasts the wait fails the build visibly; the live cache stays in service.
    live = cache_file(brain)
    monkeypatch.setattr(index, "_PUBLISH", 0)
    with index.database(brain) as (connection, _state), pytest.raises(BusyError, match="readers kept"):
        index.refresh(brain, full=True)
    assert not (brain.root / index.BUILD).exists()
    assert cache_file(brain) == live
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"


def test_rewording_schema_documentation_keeps_the_cache(brain: Store) -> None:
    schema = b"version: 7\nname: fixture\nfields:\n  owner:\n    description: Who owns it.\n    type: identity\n"
    brain.write("bf.yaml", schema + b"    relation: true\n")
    index.refresh(brain)
    generation = cache_file(brain)
    reworded = schema.replace(b"Who owns it.", b"The accountable owner.")
    brain.write("bf.yaml", reworded + b"    relation: true\n    examples: [person:ada]\n")
    assert index.refresh(brain)["changed"] == 0
    assert cache_file(brain) == generation
    # A structural change still rebuilds: edges follow relation fields.
    brain.write("bf.yaml", reworded)
    assert index.refresh(brain)["changed"] == 4
    assert cache_file(brain) != generation


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
    team.write("bf.yaml", b"version: 7\nname: team\n")
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
    assert read(stores, "bf://team/projects/offline.md")["brain"] == "team"
    assert read(stores, "meetings:lunch")["brain"] == "fixture"
    with pytest.raises(Error, match="unknown brain absent"):
        read(stores, "bf://absent/meetings:lunch")
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


def test_oversized_notes_read_as_pages_of_their_text(brain: Store) -> None:
    # Escaped quotes double the serialized size: a page holds fewer characters, never more bytes.
    data = b"# Quoted\n\n## Part\n\n" + b'"' * 3_000_000
    brain.write("concepts/quoted.md", data)
    reply = read([brain], "concepts/quoted.md")
    assert "chunk" not in reply
    assert (reply["offset"], reply["total_characters"], reply["sha256"]) == (0, len(data), digest(data))
    assert len(encode(reply)) <= pages.BUDGET
    # A large note's first page returns only its opening beside the outline; the next page fills the budget.
    assert reply["outline"]
    assert reply["next_offset"] == len(str(reply["text"]))
    assert len(encode(reply["text"])) <= retrieve.OPENING
    following = read([brain], "concepts/quoted.md", offset=cast(int, reply["next_offset"]))
    # No line ends within the slice, so it ends where the budget does.
    assert len(encode(following)) <= pages.BUDGET
    assert len(str(following["text"])) > pages.BUDGET // 4
    assert reply["modified"] == timestamp(
        # Exact integer division, as bf computes it: `ns / 1e9` rounds ns to a float first, off by one microsecond.
        datetime.fromtimestamp((brain.root / "concepts/quoted.md").stat().st_mtime_ns / 1_000_000_000, UTC).isoformat()
    )


def test_usage_counts_searches_empty_results_and_reads_without_queries(brain: Store) -> None:
    refs(brain, "offline")
    refs(brain, "zzabsent")
    read([brain], "meetings:lunch")
    search([brain], Query(text="offline"), counted=False)
    log = state_store(brain.root).root / usage.USAGE
    # Each line holds only a time, an operation and a count: never the query, ref or page.
    assert [set(json.loads(line)) for line in log.read_text().splitlines()] == [{"at", "op", "results"}] * 3
    assert not any(word in log.read_text() for word in ("offline", "zzabsent", "meetings", "lunch"))
    assert usage.summary(brain) == {
        "7d": {"search": 2, "empty": 1, "read": 1},
        "30d": {"search": 2, "empty": 1, "read": 1},
    }
    later = datetime.now(UTC) + timedelta(days=10)
    assert usage.summary(brain, now=later)["7d"] == {"search": 0, "empty": 0, "read": 0}
    log.write_text(log.read_text() + "not json\n" + '{"at": "x"}\n')
    assert usage.summary(brain)["30d"]["search"] == 2
    # Rotation drops events older than the largest window: only the new event remains.
    log.write_bytes(b'{"at":"2026-01-01T00:00:00+00:00","op":"search","results":1}\n' * 40_000)
    usage.note(brain, "search", 3)
    assert len(log.read_text().splitlines()) == 1
    log.unlink()
    log.mkdir()
    usage.note(brain, "search", 1)  # an unwritable log never breaks retrieval
    assert usage.summary(brain)["7d"]["search"] == 0


def test_identity_relations_keep_newest_first(brain: Store) -> None:
    records_file(
        brain,
        "updates",
        [
            Record(id="a", title="Old", links=["repo:unique-evidence"], time="2026-09-01T00:00:00Z"),
            Record(id="b", title="New", links=["repo:unique-evidence"], time="2026-09-23T00:00:00Z"),
        ],
    )
    assert refs(brain, "repo:unique-evidence") == ["updates:b", "updates:a"]


def test_malformed_link_does_not_block_other_notes(brain: Store) -> None:
    brain.write("projects/bad-url.md", b'---\nlinks: ["https://["]\n---\n# Invalid link\n')
    brain.write("projects/hub.md", b"# Hub\n\nOpen\n[the console](http://[fe80::1%eth0/admin).\n")
    # A document copied from another tool stays searchable without a URL Python cannot parse, such as a placeholder.
    brain.write("actions/2026-09-27_import/ACTION.md", b"---\ntype: action\n---\n# Import\n")
    brain.write(
        "actions/2026-09-27_import/inputs/vendor.md", b"# Vendor\n\nOpen [the okapi console](http://[host]:8080/).\n"
    )
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    assert refs(brain, "okapi") == ["actions/2026-09-27_import/inputs/vendor.md"]
    # Before, both problems read only "invalid link": each now names the line or frontmatter key to repair.
    assert index.status(brain)["problems"] == [
        {"file": "projects/bad-url.md", "error": "links: invalid link"},
        {"file": "projects/hub.md", "error": "line 4: invalid link"},
    ]


def test_removing_a_misnamed_record_clears_its_problem(brain: Store) -> None:
    records_file(brain, "duplicates", [Record(id="same", title="Needle")])
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
    def unavailable(_store: Store, _counts: dict[str, int] | None = None) -> str:
        raise Error("cache unavailable")

    monkeypatch.setattr(index, "fresh", unavailable)
    brain.write(
        "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json",
        b"malformed unrelated newer record\n",
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
        [Record(id="decision-1", title="Evidence from another source", time="2026-09-01T00:00:00Z")],
    )
    brain.write(
        "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json",
        b"malformed unrelated newer record\n",
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
        b"version: 7\nname: fixture\nsensors:\n  meetings:\n    command: [true-command]\n    refresh: 3600\n  disabled:\n    command: [true-command]\n    enabled: false\n",
    )
    for source in ("disabled", "historical"):
        records_file(brain, source, [Record(id="x", title="Offline retrieval")])
    for source, expected in (("meetings", "active"), ("disabled", "disabled"), ("historical", "historical")):
        result = search([brain], Query(text="offline", **scope(f"memories/{source}")))
        sources = result["sources"]
        assert isinstance(sources, list)
        assert sources[0]["state"] == expected
    # A source that no brain indexes or configures is unknown, like its page, not an empty historical source.
    with pytest.raises(NotFoundError, match="unknown source"):
        search([brain], Query(text="offline", **scope("memories/absent")))
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
    def fail(_store: Store, _config: Config) -> sqlite3.Connection:
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


def test_other_does_not_outrank_the_subject_in_a_mostly_english_brain(brain: Store) -> None:
    # The subject appears in several notes, so it weighs less than a word only one French note holds.
    brain.write("concepts/kotlin-first.md", b"# Kotlin first\n\nKotlin is the default language for services.\n")
    for name in ("build", "mobile", "style"):
        brain.write(f"concepts/{name}.md", f"# {name.title()}\n\nThe {name} guide covers Kotlin.\n".encode())
    brain.write("concepts/contact.md", "# Contact\n\nVoir les autres échanges dans le dossier.\n".encode())
    # Before, autre stayed a term and stemmed to the French note's autres, which ranked first; other is its English
    # counterpart.
    question = "Pourquoi Kotlin plutôt qu\u2019un autre langage ?"  # with the typographic apostrophe
    assert index.terms(question) == ["Kotlin", "plutôt", "langage"]
    assert index.terms("Why Kotlin over another language or other ones?") == ["Kotlin", "over", "language"]
    assert index.terms("ma liste et ta liste") == ["liste"]  # like mon and ton
    assert refs(brain, question)[0] == "concepts/kotlin-first.md"
    assert refs(brain, "autres") == ["concepts/contact.md"]  # all-stopword queries remain literal


def test_recent_identity_keeps_owners_ahead_of_newer_relations_across_brains(brain: Store, tmp_path: Path) -> None:
    records_file(
        brain,
        "updates",
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
    team.write("bf.yaml", b"version: 7\nname: team\n")
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


def test_mutual_record_aliases_resolve_one_hop(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records_file(brain, "jira", [Record(id="ABC-1", title="Issue", aliases=["github:o/r#1"])])
    records_file(brain, "github", [Record(id="o/r#1", title="Mirror", aliases=["jira:ABC-1"])])
    assert read([brain], "github:o/r#1")["ref"] == "github:o/r#1"
    # Both files vanish between the cache check and the read: each alias names the other, which never recurses.
    monkeypatch.setattr(retrieve, "_record", lambda *_args, **_kwargs: None)
    with pytest.raises(Error):
        read([brain], "jira:ABC-1")


def test_search_reports_omitted_files_and_isolates_unavailable_brains(brain: Store, tmp_path: Path) -> None:
    brain.write("projects/broken.md", b"---\nstale_after: typo\n---\n# Hidden answer\n")
    broken = tmp_path / "broken"
    broken.mkdir()
    other = Store(broken)
    other.write("bf.yaml", b"version: 7\nname: interrupted\n")
    other.write("memories/.pending/0.before", b"preserve this original")
    reply = search([brain, other], Query(text="offline retrieval"), counted=False)
    assert isinstance(reply["items"], list)
    assert isinstance(reply["problems"], list)
    assert reply["items"][0]["brain"] == "fixture"
    assert reply["problems"][0]["brain"] == "fixture"
    assert reply["problems"][0]["file"] == "projects/broken.md"
    assert reply["problems"][1]["brain"] == "interrupted"
    assert "interrupted transaction" in reply["problems"][1]["error"]
    assert other.read("memories/.pending/0.before") == b"preserve this original"
    empty = search([brain], Query(text="hidden answer"), counted=False)
    assert not empty["items"]
    assert empty["problems"]
    # An exact read names the broken brain, which could hold a competing record, beside the one that answers.
    found = read([brain, other], "meetings:lunch")
    assert found["brain"] == "fixture"
    assert "interrupted transaction" in str(cast("list[dict[str, object]]", found["problems"]))
    with pytest.raises(Error, match="interrupted transaction"):
        read([other], "meetings:lunch")
    other.write("bf.yaml", b"version: 7\nname: [invalid]\n")
    assert search([brain, other], Query(text="offline"), counted=False)["items"]
    with pytest.raises(Error, match=r"invalid bf\.yaml"):
        search([other, other], Query(text="offline"), counted=False)


def test_a_search_opens_a_generation_published_after_its_freshness_check(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    live = cache_file(brain)
    checked, reopen = Event(), Event()
    original_fresh = index.fresh

    def paused_fresh(store: Store, counts: dict[str, int] | None = None) -> str:
        state = original_fresh(store, counts)
        checked.set()
        assert reopen.wait(10)
        return state

    monkeypatch.setattr(index, "fresh", paused_fresh)
    with ThreadPoolExecutor(max_workers=1) as executor:
        query = executor.submit(search, [brain], Query(text="offline"), counted=False)
        try:
            assert checked.wait(10)
            # A checked reader holds no connection yet, so the build publishes without waiting for it.
            assert index.refresh(brain, full=True)["files"] == 4
        finally:
            reopen.set()
        reply = query.result(timeout=10)
    assert cache_file(brain) != live
    assert isinstance(reply["items"], list)
    assert reply["items"][0]["ref"] == "projects/offline.md"
    assert "problems" not in reply


def test_evaluation_names_missing_retrieval_cases(brain: Store) -> None:
    from bf.evaluate import evaluate

    with pytest.raises(Error, match="evals has no suites"):
        evaluate(brain)
    brain.write(
        "evals/retrieval.yaml", b"version: 7\ncases:\n  - name: later\n    query: x\n    scope: soon\n    empty: true\n"
    )
    with pytest.raises(Error, match=r"invalid evals/retrieval\.yaml: cases\.0\.scope: scope accepts"):
        evaluate(brain)
    for case in (b"    query: x\n    read: today\n", b"    read: today\n    scope: 7d\n", b"    since: 7d\n"):
        brain.write("evals/retrieval.yaml", b"version: 7\ncases:\n  - name: bad\n" + case + b"    empty: true\n")
        with pytest.raises(Error, match=r"case bad|since"):
            evaluate(brain)
    # A query that bf search rejects as invalid input fails while loading too, before any retrieval.
    for text, reason in (
        ("bf:foo", "invalid BF link"),
        ("bf://fixture/projects/offline.md?rel=owner", "identity must"),
    ):
        brain.write(
            "evals/retrieval.yaml",
            f"version: 7\ncases:\n  - name: typed\n    query: {text}\n    empty: true\n".encode(),
        )
        with pytest.raises(Error, match=rf"invalid evals/retrieval\.yaml: cases\.0: case typed: {reason}"):
            evaluate(brain)


def test_evaluation_rejects_incomplete_empty_answers(brain: Store) -> None:
    from bf.evaluate import evaluate

    brain.write("projects/broken.md", b"---\nstale_after: typo\n---\n# Lost answer\n")
    brain.write(
        "evals/retrieval.yaml", b"version: 7\ncases:\n  - name: absent\n    query: lost answer\n    empty: true\n"
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

    records_file(brain, "issues", [Record(id="item#comment", title="Hashneedle")])
    brain.write(
        "evals/retrieval.yaml",
        b"version: 7\ncases:\n  - name: exact-record\n    query: hashneedle\n    expect: [issues:item]\n",
    )
    assert not evaluate(brain)["passed"]


def test_one_ranked_query_keeps_notes_above_long_records_that_happen_to_hold_every_word(brain: Store) -> None:
    # The note holds three of the four words; every trace holds all four in a long body.
    brain.write("projects/ranking.md", b"# Search ranking\n\nThe framework orders results lexically.\n")
    filler = " ".join(f"unrelated{i}" for i in range(400))
    records_file(
        brain,
        "traces",
        [
            Record(id=f"t{i}", title=f"Trace {i}", text=f"{filler} brain {filler} framework search ranking")
            for i in range(12)
        ],
    )
    # Before 12.0.0 every item holding all four words ranked first, so these traces hid the note entirely.
    assert refs(brain, "brain framework search ranking", limit=3)[0] == "projects/ranking.md"


def test_a_section_ranks_under_its_note_title_while_the_title_alone_finds_the_note(brain: Store) -> None:
    brain.write("projects/atlas.md", b"# Atlas\n\nA fictional project.\n\n## Next actions\n\n- Measure latency.\n")
    for name in ("borealis", "cobalt", "delta"):
        body = f"# {name.title()}\n\n## Next actions\n\n- Compare with the Atlas baseline.\n"
        brain.write(f"projects/{name}.md", body.encode())
        brain.write(f"actions/2026-09-20_{name}/ACTION.md", b"# Session\n\n## Resume\n\nAtlas next actions first.\n")
    # Before 15.0.0 the heading alone ranked a section: every other "Next actions" mentioning Atlas came first.
    assert refs(brain, "Atlas next actions")[0] == "projects/atlas.md#next-actions"
    # A section matching only through its note's title repeats the note, which answers instead.
    assert refs(brain, "Atlas")[0] == "projects/atlas.md"
    assert refs(brain, "Atlas latency")[0] == "projects/atlas.md#next-actions"
    # Before 17 the title also ranked as a heading of every section: one mentioning it once outranked the note.
    brain.write(
        "projects/zephyr.md",
        b"---\ntype: project\nsummary: A launch.\n---\n# Zephyr\n\nThe launch project, owned by the platform team.\n\n"
        b"## Status\n\nZephyr ships in October.\n\n## Risks\n\nZephyr depends on one vendor.\n",
    )
    assert refs(brain, "Zephyr")[0] == "projects/zephyr.md"
    assert refs(brain, "Zephyr vendor")[0] == "projects/zephyr.md#risks"


def test_quoted_phrases_and_word_prefixes_match_while_other_syntax_stays_literal(brain: Store) -> None:
    brain.write("concepts/sync.md", b"# Sync\n\nThe offices synchronize field reports nightly.\n")
    brain.write("projects/plan.md", b"# Plan\n\nThe launch budget is fixed.\n")
    brain.write("projects/memo.md", b"# Memo\n\nThe budget covers the launch.\n")
    # Before 16 the `*` and quotes were dropped: `synchro` matched no word, and both notes held the two words.
    assert refs(brain, "synchro*") == ["concepts/sync.md"]
    assert refs(brain, '"launch budget"') == ["projects/plan.md"]
    assert refs(brain, "“launch budget”") == ["projects/plan.md"]
    assert refs(brain, '"budget"') == refs(brain, "budget")
    # A phrase keeps its function words; an unclosed quote leaves plain words.
    assert refs(brain, '"covers the launch"') == ["projects/memo.md"]
    assert set(refs(brain, '"launch budget')) == {"projects/plan.md", "projects/memo.md"}
    assert index.terms('Synchro* "the launch  budget" the "" sync*') == ["Synchro*", "the launch budget", "sync*"]
    # FTS5 operators, column filters and stray quotes are only words: nothing reaches SQLite as syntax.
    for query in (
        "NEAR(launch budget)",
        "launch AND budget",
        "title:launch",
        "^launch",
        '"launch',
        "launch*budget",
        "-launch",
    ):
        assert "projects/plan.md" in refs(brain, query), query
    assert refs(brain, "repo:example/project") == ["projects/offline.md", "meetings:decision-1"]


def test_search_names_query_words_that_match_nothing(brain: Store, tmp_path: Path) -> None:
    reply = search([brain], Query(text="offline budjet"))
    # Before 16 a misspelled word left no trace: the reply looked like an answer about both words.
    assert reply["unmatched"] == ["budjet"]
    assert "projects/offline.md" in refs(brain, "offline budjet")
    assert "unmatched" not in search([brain], Query(text="offline retrieval"))
    assert search([brain], Query(text='"retrieval offline" synchro* zzabsent'))["unmatched"] == [
        '"retrieval offline"',
        "synchro*",
        "zzabsent",
    ]
    # Dropped function words are not reported; a scope does not make a word unknown to the brain.
    assert search([brain], Query(text="what is the zzabsent"))["unmatched"] == ["zzabsent"]
    assert "unmatched" not in search([brain], Query(text="lunch offline", **scope("concepts")))
    # Identities, known or not, are never checked as words.
    assert "unmatched" not in search([brain], Query(text="repo:example/project"))
    # A word counts as matched when any selected brain holds it.
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    team.write("concepts/budget.md", b"# Budjet\n\nA misspelled title.\n")
    assert "unmatched" not in search([brain, team], Query(text="offline budjet"))


def test_tags_rank_like_headings_above_passing_mentions(brain: Store) -> None:
    brain.write("projects/launch.md", b"---\ntype: project\ntags: [vega]\n---\n# Launch\n\nThe partner release plan.\n")
    brain.write(
        "concepts/stars.md", b"---\ntype: concept\n---\n# Stars\n\nSeveral stars, such as Vega, shine in autumn.\n"
    )
    # Before 16 tags ranked at half the weight of body text, so a passing mention came first.
    assert refs(brain, "vega") == ["projects/launch.md", "concepts/stars.md"]


def test_nested_sections_rank_and_read_under_their_parent_headings(brain: Store) -> None:
    brain.write(
        "projects/portfolio.md",
        b"# Portfolio\n\n## Orion\n\n### Budget\n\nApproved at 2600 credits.\n\n"
        b"## Vega\n\nA partner survey.\n\n### Budget\n\nApproved at 1300 credits.\n",
    )
    # Before 16 a section ranked with its own heading and note title only: `#vega` answered, without the amount.
    item = cast("list[dict[str, str]]", search([brain], Query(text="Vega budget"))["items"])[0]
    assert (item["ref"], item["title"]) == ("projects/portfolio.md#budget-1", "Portfolio — Vega — Budget")
    assert item["excerpt"] == "Approved at 1300 credits."
    assert refs(brain, "Orion budget")[0] == "projects/portfolio.md#budget"
    # A parent heading alone never ranks a child: the parent section answers.
    assert refs(brain, "Vega") == ["projects/portfolio.md#vega"]


def test_a_later_h1_ends_the_section_search_lands_on(brain: Store) -> None:
    # A copied document can hold several H1s. Before 18.1.4 text under a later H1 ranked as the H2 above it, whose
    # exact read stops at that H1 and so lacked the match.
    brain.write(
        "projects/manual.md",
        b"# Manual\n\n## Alpha\n\nAlpha setup.\n\n# Appendix\n\nThe quagga table.\n\n### Rows\n\nZebra rows.\n",
    )
    item = cast("list[dict[str, str]]", search([brain], Query(text="quagga"))["items"])[0]
    assert (item["ref"], item["excerpt"]) == ("projects/manual.md#appendix", "The quagga table.")
    assert "quagga" in cast(str, read([brain], item["ref"])["text"])
    item = cast("list[dict[str, str]]", search([brain], Query(text="zebra"))["items"])[0]
    assert (item["ref"], item["title"]) == ("projects/manual.md#rows", "Manual — Appendix — Rows")
    assert "quagga" not in cast(str, read([brain], "projects/manual.md#alpha")["text"])
    # The title's own H1 after a section ends that section too.
    brain.write("projects/guide.md", b"## Preface\n\nPreface words.\n\n# Guide\n\nThe okapi table.\n")
    item = cast("list[dict[str, str]]", search([brain], Query(text="okapi"))["items"])[0]
    assert "okapi" in cast(str, read([brain], item["ref"])["text"])


def test_whole_note_results_without_matching_text_preview_the_note_lead(brain: Store) -> None:
    brain.write("projects/atlas.md", b"---\ntype: project\n---\n# Atlas\n\n## Decision\n\nAtlas uses SQLite.\n")
    item = cast("list[dict[str, str]]", search([brain], Query(text="Atlas"))["items"])[0]
    # Before 16 the note's empty introduction left the excerpt empty.
    assert (item["ref"], item["excerpt"]) == ("projects/atlas.md", "Atlas uses SQLite.")


def test_note_leads_keep_identifiers_in_listings_and_title_matches(brain: Store) -> None:
    brain.write(
        "projects/deploy.md",
        b"---\ntype: project\n---\n# Zebra deploy\n\n## Steps\n\nExport `GITHUB_TOKEN`, run deploy_all.sh on 2*3 hosts.\n",
    )
    lead = "Export GITHUB_TOKEN, run deploy_all.sh on 2*3 hosts."
    # Before, leads dropped every _ and *, so an agent copied GITHUBTOKEN and deployall.sh, which match nothing.
    listed = cast("list[dict[str, str]]", read([brain], "projects")["items"])
    assert next(item for item in listed if item["ref"] == "projects/deploy.md")["excerpt"] == lead
    item = cast("list[dict[str, str]]", search([brain], Query(text="zebra"))["items"])[0]
    assert (item["ref"], item["excerpt"]) == ("projects/deploy.md", lead)


def test_results_name_other_matching_sections_of_their_note(brain: Store, tmp_path: Path) -> None:
    item = cast("list[dict[str, object]]", search([brain], Query(text="retention guide"))["items"])[0]
    # Before 16 only the best section of a note returned; the decision was hidden.
    assert (item["ref"], item["sections"]) == ("projects/offline.md#next-actions", ["projects/offline.md#decision"])
    body = "# Quarters\n\n" + "".join(f"## Q{n}\n\n{'Revenue grew. ' * n}Revenue grew.\n\n" for n in range(1, 6))
    brain.write("projects/quarters.md", body.encode())
    item = cast("list[dict[str, object]]", search([brain], Query(text="revenue"))["items"])[0]
    # Best first, at most three, never the result itself or the whole note.
    assert item["ref"] == "projects/quarters.md#q5"
    assert item["sections"] == [f"projects/quarters.md#q{n}" for n in (4, 3, 2)]
    assert "sections" not in cast("list[dict[str, object]]", search([brain], Query(text="lunch"))["items"])[0]
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    both = cast("list[dict[str, object]]", search([brain, team], Query(text="retention guide"))["items"])
    assert both[0]["sections"] == ["bf://fixture/projects/offline.md#decision"]


def test_function_words_of_questions_drop_while_subjects_stay(brain: Store) -> None:
    assert index.terms("Has there been any update on the budget, and what should we do about it?") == [
        "update",
        "budget",
    ]
    assert index.terms("Pourquoi n'y a-t-il pas de budget, et qu'ont-ils été si sûrs ?") == ["budget", "sûrs"]
    assert index.terms("has been") == ["has", "been"]
    brain.write("projects/budget.md", b"# Budget\n\nThe budget is final.\n")
    brain.write("projects/noise.md", b"# Has been\n\nThere has been any number of notes about it.\n")
    # Before 16 these auxiliary words ranked the noise first.
    assert refs(brain, "Has there been any budget?")[0] == "projects/budget.md"


def test_words_that_stem_to_function_words_drop(brain: Store) -> None:
    # English stemming turns one into on, doing into do and ours into our: as terms they match nearly every text.
    assert index.terms("why did we choose one page") == ["choose", "page"]
    assert index.terms("ones doing having ours yours theirs hers budget") == ["budget"]
    brain.write("projects/website.md", b"# Website\n\n## Decision\n\nStart with a single product page.\n")
    brain.write("projects/hosting.md", b"# Hosting\n\nThe page runs on a vendor platform, on call on weekends.\n")
    # Before, the hosting note ranked first: each of its "on" matched "one".
    assert refs(brain, "why did we choose one page")[0] == "projects/website.md#decision"


def test_equal_scores_list_the_newest_first(brain: Store, tmp_path: Path) -> None:
    def status(name: str, time: str = "") -> Record:
        return Record(id=name, title="Weekly status", text="Status call.", time=time)

    records_file(
        brain,
        "calendar",
        [
            status("a", "2026-09-01T09:00:00Z"),
            status("b", "2026-09-15T09:00:00Z"),
            status("c", "2026-09-08T09:00:00Z"),
            status("d"),
        ],
    )
    # Before 16 ties listed refs alphabetically: the oldest meeting came first.
    assert refs(brain, "weekly status") == ["calendar:b", "calendar:c", "calendar:a", "calendar:d"]
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    records_file(team, "calendar", [status("e", "2026-09-22T09:00:00Z")])
    both = cast("list[dict[str, object]]", search([brain, team], Query(text="weekly status"))["items"])
    # Scores of different brains do not compare: each rank takes the newest brain's item first.
    assert [(item["brain"], item["ref"]) for item in both][:3] == [
        ("team", "calendar:e"),
        ("fixture", "calendar:b"),
        ("fixture", "calendar:c"),
    ]


def test_evaluation_ranks_expected_refs_beyond_the_case_limit(brain: Store) -> None:
    from bf.evaluate import evaluate

    brain.write("concepts/retention.md", b"# Retention decision\n\nA retention decision.\n")
    brain.write(
        "evals/retrieval.yaml",
        b"version: 7\ncases:\n  - name: decision\n    query: retention decision\n    limit: 1\n"
        b"    expect: [projects/offline.md#decision]\n",
    )
    reply = evaluate(brain)
    case = cast("list[dict[str, object]]", reply["cases"])[0]
    # Before 16 a ref below the limit ranked as missing: MRR could not tell rank 2 from rank 50.
    assert (case["passed"], case["rank"], case["returned"]) == (
        False,
        {"projects/offline.md#decision": 2},
        ["concepts/retention.md"],
    )
    assert reply["mrr"] == 0.5


def test_records_of_several_sources_sharing_a_url_collapse_to_the_best_ranked_one(brain: Store, tmp_path: Path) -> None:
    url = "https://docs.example.test/plan"
    records_file(brain, "drive", [Record(id="plan", title="Launch plan", text="Launch after review.", url=url)])
    for n in range(7):
        records_file(
            brain, f"mirror{n}", [Record(id="plan", title="Launch plan", url=url, links=["repo:example/plan"])]
        )
    # One source's records of a URL stay distinct, such as two highlights of one document.
    book = "https://book.example.test/"
    records_file(brain, "highlights", [Record(id=f"h{n}", title=f"Launch plan note {n}", url=book) for n in (1, 2)])
    items = cast("list[dict[str, object]]", search([brain], Query(text="launch plan"))["items"])
    shared = [item for item in items if item.get("url") == url]
    assert len(shared) == 1
    also = cast("list[str]", shared[0]["also"])
    # At most five other matching refs, sorted; all eight records remain distinct evidence.
    assert len(also) == index.ALSO
    assert also == sorted(also)
    assert {shared[0]["ref"], *also} < {"drive:plan", *(f"mirror{n}:plan" for n in range(7))}
    assert sorted((str(item["ref"]), "also" in item) for item in items if item.get("url") == book) == [
        ("highlights:h1", False),
        ("highlights:h2", False),
    ]
    # Collapse precedes the window, so continuations neither repeat nor skip a result.
    windows = [refs(brain, "launch plan", limit=1, offset=offset) for offset in range(len(items) + 1)]
    assert [ref for window in windows for ref in window] == [str(item["ref"]) for item in items]
    # Identity searches list every record that links to the identity.
    assert len(refs(brain, "repo:example/plan", limit=50)) == 7
    # Only one brain's records collapse; another brain keeps its own copy.
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    records_file(team, "drive", [Record(id="plan", title="Launch plan", url=url)])
    both = cast("list[dict[str, object]]", search([brain, team], Query(text="launch plan"))["items"])
    assert sorted(str(item["brain"]) for item in both if item.get("url") == url) == ["fixture", "team"]
    # With several brains, `also` names records by address: a plain ref could exist in both.
    mine = next(item for item in both if item.get("url") == url and item["brain"] == "fixture")
    assert cast("list[str]", mine["also"]) == [f"bf://fixture/{ref}" for ref in also]


def test_low_priority_sources_rank_at_half_weight_without_a_rebuild(brain: Store) -> None:
    records_file(brain, "news", [Record(id="bridge", title="Harbor bridge", text="Harbor bridge.")])
    records_file(
        brain, "minutes", [Record(id="bridge", title="Harbor bridge", text="The harbor bridge needs repairs.")]
    )
    assert refs(brain, "harbor bridge") == ["news:bridge", "minutes:bridge"]
    cache = (brain.root / index.CACHE).stat().st_ino
    brain.write(
        "bf.yaml", b"version: 7\nname: fixture\nsensors:\n  news:\n    command: [news-cli]\n    priority: low\n"
    )
    assert refs(brain, "harbor bridge") == ["minutes:bridge", "news:bridge"]
    assert (brain.root / index.CACHE).stat().st_ino == cache
    # A source scope holds one priority: its order and pages stay unchanged.
    assert refs(brain, "harbor", **scope("memories/news")) == ["news:bridge"]


def test_only_entry_notes_rank_above_evidence(brain: Store) -> None:
    assert entry_note("projects/archive.md")
    assert entry_note("concepts/retention.md")
    assert entry_note("actions/2026-09-25_review/ACTION.md")
    for working in (
        "projects/index.md",
        "projects/log.md",
        "projects/team/log.md",
        "concepts/index.md",
        "concepts/log.md",
        "concepts/team/index.md",
        "actions/2026-09-25_review/inputs/request.md",
        "actions/2026-09-25_review/outputs/ACTION.md",
    ):
        assert not entry_note(working)
    # An OKF bundle index navigates its notes: it gets no boost over the project it lists.
    brain.write("projects/index.md", b"# Projects\n\n- [Offline retrieval](offline.md)\n")
    refs(brain, "offline")
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        weights = dict(connection.execute("SELECT ref,weight FROM items WHERE kind='note'").fetchall())
    assert weights == {"projects/offline.md": 2, "projects/index.md": 1, "concepts/evidence.md": 2}


def test_a_known_identity_ignores_other_brains_words(brain: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    team.write("projects/words.md", b"# Repo example project\n\nOnly words, no link.\n")
    reply = search([brain, team], Query(text="repo:example/project"))
    assert "identity" not in reply
    assert {(i["brain"], i["ref"]) for i in cast("list[dict[str, object]]", reply["items"])} == {
        ("fixture", "projects/offline.md"),
        ("fixture", "meetings:decision-1"),
    }


def test_query_words_fold_like_the_indexed_text(brain: Store) -> None:
    # A PDF ligature and full-width letters, as extracted documents and East Asian input methods produce them.
    fullwidth = "".join(chr(ord(letter) + 0xFEE0) for letter in "ATRIUM")
    brain.write("concepts/street.md", f"# Hauptstraße\n\nDie ﬁnance team meets in the {fullwidth}.\n".encode())
    brain.write("concepts/summer.md", "# Budget\n\nLe budget de l'été.\n".encode())
    for query in ("Hauptstraße", "HAUPTSTRAẞE", "finance", "ﬁnance", "atrium"):
        assert refs(brain, query) == ["concepts/street.md"], query
    decomposed = unicodedata.normalize("NFD", "été")
    assert decomposed != "été"
    assert refs(brain, decomposed) == refs(brain, "été") == ["concepts/summer.md"]
    assert index.terms("Août août AOUT the") == ["Août", "AOUT"]


def test_queries_without_words_are_invalid_input(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = server([brain])
    for query in ("", " ", "!!!", "—", "_"):
        with pytest.raises(ValidationError, match="give words or an identity"):
            Query(text=query)
        monkeypatch.setattr(sys, "argv", ["bf", "search", query, "--brain", str(brain.root)])
        with pytest.raises(SystemExit) as exited:
            main()
        assert exited.value.code == 2
        result = asyncio.run(mcp.call_tool("search", {"query": query}))
        assert isinstance(result, CallToolResult)
        assert result.is_error
        assert "invalid" in str(result.content)


def test_excerpts_keep_the_evidence_characters(brain: Store) -> None:
    body = "The ﬁnal budget is 10 m² … wait™ for approval."
    brain.write("concepts/budget.md", f"# Plan\n\n{body}\n".encode())
    padding = " ".join(["filler"] * 80)
    brain.write("concepts/ration.md", f"# Ration\n\n{padding} the ﬁnal ½ ration {padding}\n".encode())
    reply = search([brain], Query(text="final"))
    excerpts = {item["ref"]: item["excerpt"] for item in cast("list[dict[str, str]]", reply["items"])}
    assert excerpts["concepts/budget.md"] == body
    assert excerpts["concepts/ration.md"].startswith("… filler")
    assert excerpts["concepts/ration.md"].endswith("filler …")
    assert "the ﬁnal ½ ration" in excerpts["concepts/ration.md"]


def test_only_returned_results_compute_excerpts(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records_file(brain, "notes", [Record(id=f"{n:03}", title="Needle", text="needle") for n in range(60)])
    computed: list[int] = []
    original = index.excerpt

    def counted(connection: sqlite3.Connection, text: str, passage: int) -> str:
        computed.append(passage)
        return original(connection, text, passage)

    monkeypatch.setattr(index, "excerpt", counted)
    reply = search([brain], Query(text="needle", limit=5, offset=50))
    assert len(cast("list", reply["items"])) == len(computed) == 5
    assert all(item["excerpt"] == "needle" for item in cast("list[dict[str, object]]", reply["items"]))


def test_search_coverage_names_the_sources_it_returned_or_that_need_attention(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nsensors:\n"
        b"  manual:\n    command: [true-command]\n"
        b"  due:\n    command: [true-command]\n    refresh: 3600\n",
    )
    records_file(brain, "manual", [Record(id="m", title="Manual notes")])
    reply = search([brain], Query(text="offline retrieval"))
    # meetings returned a record; due never collected here; manual needs nothing and returned nothing.
    assert [(s["source"], s["freshness"]) for s in cast("list[dict]", reply["sources"])] == [
        ("due", "never"),
        ("meetings", "unknown"),
    ]
    assert reply["sources_omitted"] == 1
    # An empty reply, or a search of collected evidence alone, lists every searched source.
    empty = search([brain], Query(text="zzabsent"))
    assert [s["source"] for s in cast("list[dict]", empty["sources"])] == ["due", "manual", "meetings"]
    assert "sources_omitted" not in empty
    scoped = search([brain], Query(text="offline", **scope("memories")))
    assert len(cast("list[dict]", scoped["sources"])) == 3
    # Authored-folder scopes search no source.
    assert not {"sources", "sources_omitted"} & set(search([brain], Query(text="offline", **scope("projects"))))


def test_source_scopes_cover_only_brains_that_hold_the_source(brain: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    team.write("projects/offline.md", b"# Team offline\n\nOffline retrieval too.\n")
    reply = search([brain, team], Query(text="offline", **scope("memories/meetings")))
    assert [(source["brain"], source["source"]) for source in cast("list[dict]", reply["sources"])] == [
        ("fixture", "meetings")
    ]
    with pytest.raises(NotFoundError, match="unknown source"):
        search([brain, team], Query(text="offline", **scope("memories/meeting")))


def test_retired_schema_fields_keep_their_records_searchable(brain: Store) -> None:
    field = b"fields:\n  kind:\n    description: Kind.\n    type: string\n"
    brain.write("bf.yaml", b"version: 7\nname: fixture\n" + field)
    records_file(brain, "notes", [Record(id="a1", title="Zirconium", fields={"kind": "note"})])
    assert refs(brain, "zirconium") == ["notes:a1"]
    brain.write("bf.yaml", b"version: 7\nname: fixture\n")
    reply = search([brain], Query(text="zirconium"))
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])] == ["notes:a1"]
    assert "problems" not in reply
    # Validation still names the stored field that the schema no longer declares.
    assert "field kind is not declared in bf.yaml fields" in str(validate(brain)["problems"])


def test_brain_record_addresses_never_resolve_other_schemes_aliases(brain: Store) -> None:
    brain.write("concepts/alice.md", b"---\ntype: person\naliases: [person:email/alice@example.test]\n---\n# Alice\n")
    address = "bf://fixture/person:email/alice@example.test"
    brain.write("projects/cite.md", f"---\ntype: project\n---\n# Cite\n\n[Alice]({address})\n".encode())
    # Read, validate and the search scope agree: the address names a record, which does not exist.
    view = read([brain], address)
    assert view["page"] == address
    assert [item["ref"] for group in cast("list[dict]", view["backlinks"]) for item in group["items"]] == [
        "projects/cite.md"
    ]
    assert f"unresolved BF target: {address}" in str(validate(brain)["problems"])
    assert refs(brain, "cite", **scope(address)) == ["projects/cite.md"]
    assert read([brain], "person:email/alice@example.test")["ref"] == "concepts/alice.md"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_read_only_brains_name_the_cache_they_need(brain: Store) -> None:
    index.refresh(brain)
    folders = [brain.root / ".bf", brain.root]
    for folder in folders:
        folder.chmod(0o500)
    try:
        with pytest.raises(Error, match=r"write access to the brain.s \.bf cache"):
            search([brain], Query(text="offline"))
    finally:
        for folder in folders:
            folder.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_an_unreadable_folder_is_reported_while_the_rest_of_the_brain_answers(brain: Store) -> None:
    brain.write("projects/private/plan.md", b"# Plan\n\nOffline secrets.\n")
    brain.write("memories/jira/.keep", b"")
    folders = [brain.root / "projects/private", brain.root / "memories/jira"]
    for folder in folders:
        folder.chmod(0)
    try:
        # Before 17 one unreadable folder failed every search, status, validation and build of the brain.
        reply = search([brain], Query(text="offline"))
        assert "projects/offline.md" in [item["ref"] for item in cast("list[dict[str, str]]", reply["items"])]
        assert {problem["file"] for problem in cast("list[dict[str, str]]", reply["problems"])} == {
            "projects/private",
            "memories/jira",
        }
        problems = cast("list[dict[str, str]]", validate(brain)["problems"])
        assert {problem["file"]: problem["error"] for problem in problems} == dict.fromkeys(
            ("memories/jira", "projects/private"),
            "unreadable folder; grant read and search permission or move it out of the brain",
        )
    finally:
        for folder in folders:
            folder.chmod(0o700)


def test_hashed_inode_numbers_beyond_signed_64_bits_still_index(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    original = storage._fingerprint  # noqa: SLF001 - the one place every fingerprint passes through

    def hashed(info: os.stat_result) -> tuple[int, int, int, int]:
        # mergerfs and some FUSE mounts report inode numbers with the top bit set.
        return original(
            cast(
                "os.stat_result",
                SimpleNamespace(
                    st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns,
                    st_ctime_ns=info.st_ctime_ns,
                    st_ino=info.st_ino | 1 << 63,
                ),
            )
        )

    monkeypatch.setattr(storage, "_fingerprint", hashed)
    # Before 17 SQLite rejected the inode and every refresh failed with a traceback.
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"


def test_a_note_alias_named_like_a_source_reads_without_parsing_that_source(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("concepts/atlas.md", b"---\ntype: concept\naliases: [meetings:atlas]\n---\n# Atlas\n\nThe plan.\n")
    assert read([brain], "meetings:atlas")["ref"] == "concepts/atlas.md"
    # Before 17 another malformed record of the source, or a busy writer, failed the read of the alias.
    brain.write("memories/meetings/" + "0" * 64 + ".json", b"{broken")
    assert read([brain], "meetings:atlas")["ref"] == "concepts/atlas.md"
    monkeypatch.setattr(records, "WAIT", 0.1)
    with writer(brain):
        assert read([brain], "meetings:atlas")["ref"] == "concepts/atlas.md"
    # An exact record ref still needs its own file: a missing one of an unreadable source is not proven absent.
    with pytest.raises(Error, match="invalid JSON document"):
        read([brain], "meetings:absent")


def test_words_starting_with_a_bf_scheme_stay_words(brain: Store) -> None:
    brain.write("concepts/setup.md", b"# Setup\n\nHow to configure sensors.\n")
    # Before 17 any query starting with bf: was parsed as an address and failed as an invalid link.
    assert refs(brain, "bf: how to configure sensors") == ["concepts/setup.md"]
    assert refs(brain, "Bf:setup") == ["concepts/setup.md"]


def test_the_cache_directory_must_be_your_own_directory(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = brain.root / ".bf"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    cache.symlink_to(elsewhere)
    with pytest.raises(Error, match=r"^\.bf: expected a directory"):
        search([brain], Query(text="offline"))
    # The check never follows the link, so it tightens nothing elsewhere.
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755
    cache.unlink()
    cache.write_bytes(b"not a directory")
    with pytest.raises(Error, match=r"^\.bf: expected a directory"):
        search([brain], Query(text="offline"))
    cache.unlink()
    cache.mkdir()
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    with pytest.raises(Error, match="belongs to another user"):
        search([brain], Query(text="offline"))


def test_the_cache_is_private_and_distrusts_its_own_schema(brain: Store) -> None:
    (brain.root / ".bf").mkdir()
    (brain.root / ".bf").chmod(0o755)
    refs(brain, "offline")
    assert stat.S_IMODE((brain.root / ".bf").stat().st_mode) == 0o700
    assert stat.S_IMODE((brain.root / index.CACHE).stat().st_mode) == 0o600
    with index.database(brain) as (connection, _state):
        assert not connection.getconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA)
        assert connection.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)
