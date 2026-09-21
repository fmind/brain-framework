"""Scheduled refresh remains explicit, recoverable and idle when nothing is due."""

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from fkf.cli import app
from fkf.collect import collect
from fkf.index import CACHE, build
from fkf.models import Collection, Error, decode
from fkf.storage import Store
from fkf.update import update


def test_refresh_windows_failures_and_idle_run(base: Store) -> None:
    base.write(
        "fkf.yaml",
        b"version: 1\nid: aabbccddeeff00112233445566778899\nname: fixture\nsources:\n"
        b"  activity: {command: [echo, '{{start}}', '{{end}}'], refresh: 3600, overlap: 600}\n"
        b"  manual: {command: [echo]}\n  disabled: {command: [echo], refresh: 1, enabled: false}\n",
    )
    now = datetime(2026, 9, 19, tzinfo=UTC)
    calls: list[list[str]] = []

    def fake(argv: list[str], *_: object) -> bytes:
        calls.append(argv)
        return b'[{"id":"one","title":"A useful outcome"}]'

    before = base.files("records")
    plan = update(base, now=now, runner=fake, dry_run=True)
    assert plan["sources"] == [
        {"source": "activity", "start": (now - timedelta(days=1)).isoformat(), "end": now.isoformat(), "status": "due"}
    ]
    assert not calls
    assert base.files("records") == before
    assert update(base, now=now, runner=fake)["ok"]
    fingerprint = base.fingerprint(CACHE)
    update(base, now=now + timedelta(minutes=30), runner=fake)
    assert len(calls) == 1
    assert base.fingerprint(CACHE) == fingerprint

    def broken(*_: object) -> bytes:
        raise Error("private-provider-message")

    failed = update(base, now=now + timedelta(hours=1), runner=broken)
    assert not failed["ok"]
    assert "private-provider-message" not in str(failed)
    assert len(base.files("records")) == len(before) + 1
    assert update(base, now=now + timedelta(hours=2), runner=fake)["ok"]
    assert calls[-1][1:] == [
        (now - timedelta(minutes=10)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        (now + timedelta(hours=2)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    ]


def test_conditional_build_repairs_stale_and_corrupt_cache(base: Store) -> None:
    assert build(base, if_stale=True)["changed"]
    previous = base.fingerprint(CACHE)
    assert not build(base, if_stale=True)["changed"]
    assert base.fingerprint(CACHE) == previous
    base.write("wiki/new.md", b"# New evidence")
    assert build(base, if_stale=True)["changed"]
    base.write(CACHE, b"broken")
    assert build(base, if_stale=True)["changed"]
    runner = CliRunner()
    assert runner.invoke(app, ["build", "--base", str(base.root), "--check", "--if-stale"]).exit_code != 0
    assert runner.invoke(app, ["update", "--base", str(base.root), "--dry-run"]).exit_code == 0


def test_manual_windows_never_advance_or_delay_automatic_progress(base: Store) -> None:
    base.write(
        "fkf.yaml",
        b"id: aabbccddeeff00112233445566778899\nname: fixture\nsources:\n  activity: {command: [echo], refresh: 3600, overlap: 300}\n",
    )
    first = datetime(2026, 9, 1, tzinfo=UTC)
    later = datetime(2026, 9, 19, 12, tzinfo=UTC)

    def fake(*_: object) -> bytes:
        return b"[]"

    update(base, now=first, runner=fake)
    automatic = base.files("records/activity")[0]
    original = base.read(automatic)
    assert Collection.model_validate(decode(original)).automatic
    for start, end in [
        ("2026-09-19T11:50:00Z", "2026-09-19T12:00:00Z"),
        ("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
    ]:
        result = collect(base, "activity", start=start, end=end, runner=fake, clock=lambda: later)
        assert not Collection.model_validate(decode(base.read(str(result["path"])))).automatic
        plan = update(base, dry_run=True, now=later + timedelta(minutes=1))
        assert plan["sources"] == [
            {
                "source": "activity",
                "start": "2026-08-31T23:55:00+00:00",
                "end": "2026-09-19T12:01:00+00:00",
                "status": "due",
            }
        ]
    # Progress comes from durable captures, independently of missing/corrupt generated state.
    base.write(CACHE, b"broken")
    assert update(base, now=later + timedelta(hours=1), runner=fake)["ok"]
    assert base.read(automatic) == original
    assert update(base, dry_run=True, now=later + timedelta(hours=1, minutes=1))["sources"] == [
        {"source": "activity", "status": "not-due"}
    ]


def test_manual_only_history_does_not_seed_the_first_automatic_window(base: Store) -> None:
    base.write(
        "fkf.yaml",
        b"id: aabbccddeeff00112233445566778899\nname: fixture\nsources:\n  activity: {command: [echo], refresh: 3600, lookback: 86400}\n",
    )
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    collect(
        base, "activity", start="2026-09-19T11:50:00Z", end=now.isoformat(), runner=lambda *_: b"[]", clock=lambda: now
    )
    result = update(base, now=now, dry_run=True)
    assert result["sources"] == [
        {
            "source": "activity",
            "start": "2026-09-18T12:00:00+00:00",
            "end": "2026-09-19T12:00:00+00:00",
            "status": "due",
        }
    ]
