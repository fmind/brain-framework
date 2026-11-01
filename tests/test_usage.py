"""Usage rotation sums past days without changing the windows `bf status` reports, within a hard size bound."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bf import pages, usage
from bf.cli import app
from bf.models import Query, Record, encode, timestamp
from bf.records import path as record_path
from bf.retrieve import read, search
from bf.storage import Store, state_store
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


def event(age: timedelta, op: str = "search", results: int = 1, **extra: object) -> bytes:
    return encode({"at": timestamp((NOW - age).isoformat()), "op": op, "results": results, **extra})


def day(offset: int, **counts: object) -> bytes:
    return encode({"day": (TODAY - timedelta(days=offset)).isoformat(), "search": 0, "empty": 0, "read": 0, **counts})


def log(brain: Store) -> Path:
    return state_store(brain.root).root / usage.USAGE


def entries(brain: Store) -> list[dict[str, object]]:
    return [json.loads(line) for line in log(brain).read_bytes().splitlines()]


@pytest.mark.usefixtures("clock")
def test_rotation_sums_past_days_without_changing_any_window(brain: Store) -> None:
    log(brain).write_bytes(
        event(timedelta(days=45), kind="none") * 10
        + event(timedelta(days=20), kind="folder") * 6_000
        + event(timedelta(days=2), results=0, kind="tag", unmatched=["zzabsent"]) * 6_000
        + event(timedelta(days=2), "read", kind="note") * 2_000
        + event(timedelta(hours=1), "read", kind="home") * 100
    )
    assert log(brain).stat().st_size > usage.LIMIT
    before = usage.summary(brain, NOW)
    assert before["7d"] == {
        "search": 6_000,
        "empty": 6_000,
        "read": 2_100,
        "kinds": {"read": {"home": 100, "note": 2_000}, "search": {"tag": 6_000}},
        "empty_kinds": {"tag": 6_000},
        "empty_terms": [{"term": "zzabsent", "count": 6_000}],
    }
    assert before["30d"]["search"] == 12_000
    usage.note(brain, "read", 1, kind="section")
    # One line per past day, oldest first, then today's events as they were written.
    lines = entries(brain)
    assert log(brain).stat().st_size <= usage.KEEP
    assert [line.get("day") for line in lines[:2]] == [str(TODAY - timedelta(days=20)), str(TODAY - timedelta(days=2))]
    assert lines[1] == {
        "day": str(TODAY - timedelta(days=2)),
        "search": 6_000,
        "empty": 6_000,
        "read": 2_000,
        "kinds": {"read": {"note": 2_000}, "search": {"tag": 6_000}},
        "empty_kinds": {"tag": 6_000},
        "unmatched": {"zzabsent": 6_000},
    }
    assert len(lines) == 2 + 101
    assert all("at" in line for line in lines[2:])
    # Compaction loses nothing the windows count: only the new read differs.
    after = usage.summary(brain, NOW)
    for window in usage.WINDOWS:
        assert after[window]["read"] == before[window]["read"] + 1
        after[window]["read"] -= 1
        del after[window]["kinds"]["read"]["section"]
    assert after == before


@pytest.mark.usefixtures("clock")
def test_today_alone_beyond_the_bound_is_summed_too(brain: Store) -> None:
    log(brain).write_bytes(event(timedelta(hours=1), kind="period") * 14_000)
    assert log(brain).stat().st_size > usage.KEEP
    usage.note(brain, "search", 0, kind="period", unmatched=["zzabsent"])
    # The size limit stays a hard bound: today's events become one day line, and later ones append after it.
    assert entries(brain) == [
        {
            "day": str(TODAY),
            "search": 14_001,
            "empty": 1,
            "read": 0,
            "kinds": {"search": {"period": 14_001}},
            "empty_kinds": {"period": 1},
            "unmatched": {"zzabsent": 1},
        }
    ]
    usage.note(brain, "search", 1, kind="none")
    counts = usage.summary(brain, NOW)["7d"]
    assert (counts["search"], counts["empty"], counts.get("kinds")) == (
        14_002,
        1,
        {"search": {"none": 1, "period": 14_001}, "read": {}},
    )


def test_a_window_counts_today_and_whole_utc_days_before_it(brain: Store) -> None:
    midnight = NOW - timedelta(hours=12)
    log(brain).write_bytes(
        day(-1, search=1)  # a future day
        + event(timedelta(minutes=-1), kind="none")  # a future instant today
        + event(timedelta(hours=12), kind="none")  # today's first instant
        + day(6, search=10)
        + day(7, search=100)
        + day(29, search=1_000)
        + day(30, search=10_000)
    )
    assert midnight.date() == TODAY
    summary = usage.summary(brain, NOW)
    assert (summary["7d"]["search"], summary["30d"]["search"]) == (11, 1_111)
    # Rotation keeps exactly the days the largest window counts, and all of today's events.
    kept = usage._rotate(log(brain).read_bytes().splitlines(keepends=True), NOW)  # noqa: SLF001 - edge of the window
    assert [json.loads(line).get("day") for line in kept.splitlines()] == [
        str(TODAY - timedelta(days=29)),
        str(TODAY - timedelta(days=7)),
        str(TODAY - timedelta(days=6)),
        None,
        None,
    ]


def test_malformed_lines_and_unknown_kinds_never_hide_the_rest(brain: Store) -> None:
    log(brain).write_bytes(
        b"not json\n[1]\n"
        + day(1, search=-1)
        + day(1, search=1, empty=1, kinds={"write": {"x": 1}})
        + encode({"day": "yesterday", "search": 1, "empty": 0, "read": 0})
        + day(1, search=2, empty=5, kinds={"search": {"folder": 1, "projects/secret.md": 1}}, unmatched={"z" * 65: 1})
        + event(timedelta(hours=1), "read", kind="projects/secret.md")
    )
    counts = usage.summary(brain, NOW)["7d"]
    # A day cannot hold more empty searches than searches; unknown kinds and unbounded tokens are dropped.
    assert counts == {"search": 2, "empty": 2, "read": 1, "kinds": {"read": {}, "search": {"folder": 1}}}
    usage.note(brain, "read", 1, kind="projects/secret.md")
    usage.note(brain, "write", 1, kind="none")
    assert [json.loads(line)["kind"] for line in log(brain).read_bytes().splitlines()[-2:]] == [usage.OTHER] * 2


@pytest.mark.usefixtures("clock")
def test_legacy_events_and_rotation_mark_still_count(brain: Store) -> None:
    since = NOW - timedelta(days=10)
    mark = encode({"since": timestamp(since.isoformat())})
    # An older release wrote events without a kind and, when it dropped events, a first line naming the oldest kept.
    log(brain).write_bytes(mark + event(timedelta(days=10)) * 17_000 + event(timedelta(hours=1), "read"))
    summary = usage.summary(brain, NOW)
    assert summary["7d"] == {"search": 0, "empty": 0, "read": 1}
    assert {window for window, counts in summary.items() if counts.get("since") == timestamp(since.isoformat())} == {
        "30d"
    }
    assert "kinds" not in summary["30d"]
    assert summary["30d"]["search"] == 17_000
    assert log(brain).stat().st_size > usage.LIMIT
    usage.note(brain, "search", 1, kind="none")
    data = log(brain).read_bytes()
    assert data.startswith(mark)
    assert usage.summary(brain, NOW)["30d"]["search"] == 17_001
    assert usage.summary(brain, NOW)["30d"]["since"] == timestamp(since.isoformat())
    # Once the cut leaves the window, the mark no longer applies, and the next rotation drops it.
    later = NOW + timedelta(days=21)
    assert not any("since" in counts for counts in usage.summary(brain, later).values())
    assert not usage._rotate(data.splitlines(keepends=True), later).startswith(mark)  # noqa: SLF001
    # A mark only counts as the first line.
    log(brain).write_bytes(event(timedelta(hours=1)) + mark)
    assert not any("since" in counts for counts in usage.summary(brain, NOW).values())


@pytest.mark.usefixtures("clock")
def test_rotation_reads_at_most_twice_the_limit_from_a_whole_line(brain: Store) -> None:
    # Concurrent appenders can push the file past the limit before one of them rotates it.
    log(brain).write_bytes(b"x" * (3 * usage.LIMIT) + b"\n" + event(timedelta(hours=1), kind="none"))
    usage.note(brain, "search", 1, kind="none")
    assert len(entries(brain)) == 2
    assert usage.summary(brain, NOW)["7d"]["search"] == 2


def test_status_states_since_in_local_time(brain: Store) -> None:
    now = datetime.now(UTC)
    since = timestamp((now - timedelta(days=10)).isoformat())
    log(brain).write_bytes(encode({"since": since}))
    result = CliRunner().invoke(app, ["status", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output
    counts = json.loads(result.stdout)["brains"][0]["usage"]["30d"]
    # Like every reply instant: the local offset, to the second; the suite runs in UTC.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", counts["since"])


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
        read([brain], ref)
    read([brain], "projects/offline.md", rel="links")
    search([brain], Query(text="durable", prefix="projects"))
    search([brain], Query(text="zzabsent", target="repo:example/project"))
    expected: dict[str, int] = {}
    for kind in [*reads.values(), "relation"]:
        expected[kind] = expected.get(kind, 0) + 1
    counts = usage.summary(brain)["7d"]
    assert counts["kinds"] == {"read": dict(sorted(expected.items())), "search": {"folder": 1, "identity": 1}}
    assert counts["empty_kinds"] == {"identity": 1}
    data = log(brain).read_bytes()
    # Fixed kind words and a zero-result search's unmatched tokens only: no query text, ref, path or identity.
    assert b"zzabsent" in data
    for content in (b"durable", b"offline", b"retention", b"meetings", b"lunch", b"repo:", b"plan", b"fixture", b"/"):
        assert content not in data


def test_counting_never_breaks_retrieval(brain: Store) -> None:
    log(brain).mkdir(parents=True)
    usage.note(brain, "search", 1, kind="none")
    assert usage.summary(brain)["7d"] == {"search": 0, "empty": 0, "read": 0}
