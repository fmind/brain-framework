"""Local SQLite usage tracking in `.bf/usage.sqlite3`: windows, pruning, permissions and zero-leak bounds."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bf import pages, usage
from bf.cli import app
from bf.models import Query, Record
from bf.records import path as record_path
from bf.retrieve import read, search
from bf.storage import Store
from conftest import records_file

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
TODAY = NOW.date()


class Clock(datetime):
    """`note` writes at NOW, so a test never straddles a UTC midnight."""

    @classmethod
    def now(cls, tz: object = None) -> Clock:  # noqa: ARG003 - datetime.now's signature
        return cls.fromisoformat(NOW.isoformat())


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "datetime", Clock)


def db(brain: Store) -> Path:
    return brain.root / usage.USAGE


def seed_day(brain: Store, offset: int, *, search: int = 0, empty: int = 0, read: int = 0) -> None:
    if not db(brain).exists():
        usage.note(brain, "search", 1, kind="none")
        with closing(sqlite3.connect(db(brain))) as connection, connection:
            connection.execute("DELETE FROM usage_days")
            connection.execute("DELETE FROM usage_kinds")
            connection.commit()
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        day = (TODAY - timedelta(days=offset)).isoformat()
        connection.execute(
            "INSERT OR REPLACE INTO usage_days(day, search, empty, read) VALUES(?,?,?,?)",
            (day, search, empty, read),
        )
        connection.commit()


@pytest.mark.usefixtures("clock")
def test_usage_writes_private_sqlite_and_aggregates_windows(brain: Store) -> None:
    usage.note(brain, "search", 0, kind="tag", unmatched=["zzabsent"])
    usage.note(brain, "read", 1, kind="section")
    assert db(brain).is_file()
    if os.geteuid() != 0:
        assert (brain.root / ".bf").stat().st_mode & 0o777 == 0o700
        assert db(brain).stat().st_mode & 0o777 == 0o600
    assert not (brain.root / ".bf/runtime.sqlite3").exists()
    summary = usage.summary(brain, NOW)
    assert summary["7d"] == {
        "search": 1,
        "empty": 1,
        "read": 1,
        "kinds": {"read": {"section": 1}, "search": {"tag": 1}},
        "empty_kinds": {"tag": 1},
        "empty_terms": [{"term": "zzabsent", "count": 1}],
    }
    assert summary["30d"] == summary["7d"]


@pytest.mark.usefixtures("clock")
def test_windows_count_today_and_whole_utc_days_before_it_and_prune_expired(brain: Store) -> None:
    seed_day(brain, -1, search=1)  # future day: ignored
    seed_day(brain, 6, search=10)
    seed_day(brain, 7, search=100)
    seed_day(brain, 29, search=1_000)
    seed_day(brain, 30, search=10_000)  # pruned on next note()
    usage.note(brain, "search", 1, kind="none")
    summary = usage.summary(brain, NOW)
    assert (summary["7d"]["search"], summary["30d"]["search"]) == (11, 1_111)
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        days = {row[0] for row in connection.execute("SELECT day FROM usage_days").fetchall()}
    assert (TODAY - timedelta(days=30)).isoformat() not in days


@pytest.mark.usefixtures("clock")
def test_malformed_rows_and_unknown_kinds_never_hide_the_rest(brain: Store) -> None:
    usage.note(brain, "search", 1, kind="folder")
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        day1 = (TODAY - timedelta(days=1)).isoformat()
        connection.execute("INSERT INTO usage_days(day,search,empty,read) VALUES(?,?,?,?)", ("yesterday", 5, 0, 0))
        connection.execute("INSERT INTO usage_days(day,search,empty,read) VALUES(?,?,?,?)", (day1, 2, 5, 0))
        connection.execute(
            "INSERT INTO usage_kinds(day,op,kind,count,empty_count) VALUES(?,?,?,?,?)",
            (day1, "search", "projects/secret.md", 1, 1),
        )
        connection.execute("INSERT INTO usage_terms(day,term,count) VALUES(?,?,?)", (day1, "z" * 65, 1))
        connection.commit()
    counts = usage.summary(brain, NOW)["7d"]
    assert counts == {"search": 3, "empty": 2, "read": 0, "kinds": {"read": {}, "search": {"folder": 1}}}
    usage.note(brain, "read", 1, kind="projects/secret.md")
    usage.note(brain, "write", 1, kind="none")
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        rows = connection.execute("SELECT op, kind FROM usage_kinds WHERE day=?", (TODAY.isoformat(),)).fetchall()
    assert ("read", usage.OTHER) in rows
    assert not any(row[0] == "write" for row in rows)


def test_status_reports_usage_windows(brain: Store) -> None:
    usage.note(brain, "search", 0, kind="none", unmatched=["missingtoken"])
    result = CliRunner().invoke(app, ["status", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output
    counts = json.loads(result.stdout)["usage"]["30d"]
    assert counts["search"] == 1
    assert counts["empty"] == 1
    assert counts["empty_terms"] == [{"term": "missingtoken", "count": 1}]


@pytest.mark.parametrize(
    ("bounds", "kind"),
    [
        ("", "none"),
        ("7d", "period"),
        ("2026-10", "period"),
        ("projects", "folder"),
        ("actions/2026-10-01_plan", "folder"),
        ("projects/offline.md", "note"),
        ("memories", "memories"),
        ("memories/meetings", "source"),
        ("memories/meetings/7d", "source-period"),
        ("memories/meetings/undated", "source-period"),
        ("memories/meetings/" + "0" * 64 + ".json", "record"),
        ("repo:example/project", "identity"),
        ("bf://fixture/projects/offline.md", "identity"),
        ("bf://fixture/tags/retention", "tag"),
        ("7d repo:example/project", "combined"),
        ("7d projects", "combined"),
        ("7d bf://fixture/tags/retention", "combined"),
    ],
)
def test_each_search_scope_has_a_kind(bounds: str, kind: str) -> None:
    assert usage.scope(pages.query("durable", bounds)) == kind


def test_each_read_page_has_a_kind_and_the_log_keeps_no_query_or_ref(brain: Store) -> None:
    brain.write("actions/2026-10-01_plan/ACTION.md", b"---\ntype: action\nstatus: draft\n---\n# Plan\n")
    records_file(brain, "notes", [Record(id="n", title="Note", links=["repo:unowned/thing"])])
    reads = {
        "": "home",
        "today": "period",
        "tasks": "tasks",
        "tags": "tags",
        "tags/retention": "tag",
        "memories": "memories",
        "memories/meetings": "source",
        "memories/meetings/2026-08": "source-period",
        record_path("meetings", "lunch"): "record",
        "projects": "folder",
        "projects/offline.md": "note",
        "projects/offline.md#decision": "section",
        "bf://fixture/meetings:lunch": "record",
        "actions/2026-10-01_plan": "action",
        "repo:unowned/thing": "identity",
    }
    for ref in reads:
        read(brain, ref)
    read(brain, "projects/offline.md", rel="links")
    search(brain, Query(text="durable", prefix="projects"))
    search(brain, Query(text="zzabsent", target="repo:example/project"))
    expected: dict[str, int] = {}
    for kind in [*reads.values(), "relation"]:
        expected[kind] = expected.get(kind, 0) + 1
    counts = usage.summary(brain)["7d"]
    assert counts["kinds"] == {"read": dict(sorted(expected.items())), "search": {"folder": 1, "identity": 1}}
    assert counts["empty_kinds"] == {"identity": 1}
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        dumped = json.dumps(
            {
                "days": connection.execute("SELECT * FROM usage_days").fetchall(),
                "kinds": connection.execute("SELECT * FROM usage_kinds").fetchall(),
                "terms": connection.execute("SELECT * FROM usage_terms").fetchall(),
            }
        ).encode()
    # Fixed kind words and a zero-result search's unmatched tokens only: no query text, ref, path or identity.
    assert b"zzabsent" in dumped
    for content in (b"durable", b"offline", b"retention", b"meetings", b"lunch", b"repo:", b"plan", b"fixture", b"/"):
        assert content not in dumped
    assert not (brain.root / ".bf/runtime.sqlite3").exists()


def test_counting_never_breaks_retrieval(brain: Store) -> None:
    db(brain).mkdir(parents=True)
    usage.note(brain, "search", 1, kind="none")
    assert usage.summary(brain)["7d"] == {"search": 0, "empty": 0, "read": 0}
    db(brain).rmdir()
    (brain.root / ".bf").chmod(0o700)
    link = brain.root / ".bf/usage-link.sqlite3"
    link.write_bytes(b"")
    db(brain).symlink_to(link)
    usage.note(brain, "search", 1, kind="none")
    assert usage.summary(brain)["7d"] == {"search": 0, "empty": 0, "read": 0}
    db(brain).unlink()
    (brain.root / (usage.USAGE + "-wal")).symlink_to(link)
    usage.note(brain, "search", 1, kind="none")
    assert usage.summary(brain)["7d"] == {"search": 0, "empty": 0, "read": 0}


@pytest.mark.usefixtures("clock")
def test_scope_variants_and_defensive_db_guards(brain: Store) -> None:
    assert usage.scope(Query(text="x", target="person:alice", prefix="projects")) == "combined"
    assert (
        usage.scope(Query.model_construct(target="bf:bad", prefix="", since="", until="", undated=False)) == "identity"
    )

    # Unknown v0 schema with pre-existing table is never overwritten; unsupported versions fail closed.
    db(brain).parent.mkdir(exist_ok=True)
    (brain.root / ".bf").chmod(0o700)
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        connection.execute("CREATE TABLE custom(x INTEGER)")
    db(brain).chmod(0o600)
    usage.note(brain, "search", 1, kind="none")
    with closing(sqlite3.connect(db(brain))) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("custom",)]
        connection.execute("PRAGMA user_version = 99")
    usage.note(brain, "search", 1, kind="none")
    assert usage.summary(brain, NOW)["7d"] == {"search": 0, "empty": 0, "read": 0}
    db(brain).unlink()

    # World-readable usage DB is ignored cleanly on summary reads, and mid-transaction SQL errors roll back.
    usage.note(brain, "search", 1, kind="none")
    db(brain).chmod(0o644)
    assert usage.summary(brain, NOW)["7d"] == {"search": 0, "empty": 0, "read": 0}
    db(brain).chmod(0o600)
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        connection.execute(
            "CREATE TRIGGER abort_write BEFORE INSERT ON usage_kinds BEGIN SELECT RAISE(ABORT, 'fail'); END"
        )
    before = usage.summary(brain, NOW)["7d"]
    usage.note(brain, "search", 0, kind="folder", unmatched=["absent"])
    assert usage.summary(brain, NOW)["7d"] == before

    # Malformed dates, out-of-window 30d rows, and zero-count kind rows are handled cleanly by summary().
    bad_day = f"{(TODAY - timedelta(days=2)):%Y-%m}-99"
    older = (TODAY - timedelta(days=10)).isoformat()
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        connection.execute("DROP TRIGGER abort_write")
        for day_val in (bad_day, older):
            connection.execute("INSERT INTO usage_days VALUES (?, 5, 1, 2)", (day_val,))
            connection.execute("INSERT INTO usage_kinds VALUES (?, 'search', 'folder', 3, 1)", (day_val,))
            connection.execute("INSERT INTO usage_terms VALUES (?, 'ignored', 3)", (day_val,))
        connection.execute("INSERT INTO usage_kinds VALUES (?, 'search', 'folder', 0, 0)", (TODAY.isoformat(),))
    assert usage.summary(brain, NOW)["7d"] == before
    with closing(sqlite3.connect(db(brain))) as connection, connection:
        connection.execute("DELETE FROM usage_kinds")
    assert "kinds" not in usage.summary(brain, NOW)["7d"]
