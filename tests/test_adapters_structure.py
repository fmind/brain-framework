"""The complete snapshot example links folders to their parents and fails closed."""

import json
import os
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from typer.testing import CliRunner

from bf.cli import app
from bf.storage import Store
from conftest import ROOT, Provider

START, END = "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"
FOLDER = "application/vnd.google-apps.folder"


def test_drive_folders_page_completely_and_keep_parent_identity(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {
                "match": ["drive", '"pageToken": "next"'],
                "stdout": {
                    "kind": "drive#fileList",
                    "files": [{"id": "child", "name": "Research", "mimeType": FOLDER, "parents": ["root"]}],
                },
            },
            {
                "match": ["drive"],
                "stdout": {
                    "kind": "drive#fileList",
                    "nextPageToken": "next",
                    "files": [{"id": "root", "name": "Work", "mimeType": FOLDER, "parents": []}],
                },
            },
        ],
    )
    records = provider.records("google-drive-folders.py", START, END)
    assert [r.id for r in records] == ["root", "child"]
    assert records[1].links == ["drive:root"]
    assert records[0].aliases == ["drive:root"]
    # The standard part-of relation maps the same identities; the links stay for brains without it.
    assert [r.attributes["parent_refs"] for r in records] == [[], ["drive:root"]]
    params = json.loads(provider.calls("gws")[0][-1])
    assert "incompleteSearch" in params["fields"]
    assert params["corpora"] == "user"


def test_catalogs_fail_before_partial_output(provider: Provider) -> None:
    for adapter, responses in [
        (
            "google-drive-folders.py",
            [{"match": ["drive"], "stdout": {"kind": "drive#fileList", "incompleteSearch": True, "files": []}}],
        ),
        (
            "google-drive-folders.py",
            [
                {
                    "match": ["drive"],
                    "stdout": {"kind": "drive#fileList", "files": [], "nextPageToken": "loop"},
                    "repeat": True,
                }
            ],
        ),
        (
            "google-drive-folders.py",
            [
                {
                    "match": ["drive"],
                    "stdout": {
                        "kind": "drive#fileList",
                        "files": [{"id": "bad", "name": "x", "mimeType": FOLDER, "parents": [42]}],
                    },
                }
            ],
        ),
    ]:
        provider.install("gws", responses)
        result = provider.run(adapter, START, END)
        assert result.returncode == 1
        assert not result.stdout
        assert "collection failed" in result.stderr


def test_calendar_agenda_uses_future_window_and_distinct_identity(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {
                "match": ["events", "list"],
                "stdout": {
                    "kind": "calendar#events",
                    "items": [
                        {
                            "id": "appointment",
                            "summary": "Tomorrow",
                            "start": {"dateTime": "2026-09-03T10:00:00Z"},
                            "updated": "2026-09-01T12:00:00Z",
                        }
                    ],
                },
            }
        ],
    )
    records = provider.records("google-calendar.py", "primary", START, END, "--agenda-days", "30")
    params = json.loads(provider.calls("gws")[0][-1])
    assert params["timeMin"] == "2026-08-31T00:00:00+00:00"
    assert params["timeMax"] == "2026-10-02T00:00:00+00:00"
    assert records[0].aliases == ["agenda:primary/appointment"]
    assert "calendar:primary/appointment" in records[0].links
    assert records[0].updated == "2026-09-01T12:00:00.000000Z"


def test_drive_maps_selected_fields_within_record_bounds(provider: Provider) -> None:
    folder = {
        "id": "plans",
        "name": "Plans\u0007 2026",
        "mimeType": FOLDER,
        "parents": ["root"],
        "createdTime": "2026-08-01T00:00:00Z",
        "modifiedTime": "2026-09-01T12:00:00.000Z",
        "webViewLink": "https://drive.example.invalid/" + "a" * 8192,
        # An unexpected provider key with a reserved name never reaches the record.
        "observed": "not a timestamp",
    }
    provider.install("gws", [{"match": ["drive"], "stdout": {"kind": "drive#fileList", "files": [folder]}}])
    [record] = provider.records("google-drive-folders.py")
    assert (record.title, record.url, record.time) == ("Plans 2026", "", "2026-08-01T00:00:00.000000Z")
    assert record.attributes == {
        "name": "Plans\u0007 2026",
        "parents": ["root"],
        "parent_refs": ["drive:root"],
        "created": "2026-08-01T00:00:00Z",
        "updated": "2026-09-01T12:00:00.000000Z",
    }


def test_calendar_keeps_records_within_bounds(provider: Provider) -> None:
    event = {
        "id": "plan",
        "summary": "Plan\u0007\nreview " + "x" * 5000,
        "htmlLink": "https://calendar.example.invalid/" + "a" * 8192,
        "source": {"url": "https://example.invalid/\u0085source", "title": "Source"},
        "attachments": [{"fileUrl": "https://example.invalid/agenda", "title": "Agenda"}],
        "updated": "yesterday",
    }
    provider.install("gws", [{"match": ["events"], "stdout": {"kind": "calendar#events", "items": [event]}}])
    [record] = provider.records("google-calendar.py", "team calendar", START, END)
    assert record.title.startswith("Plan review x")
    assert len(record.title) == 4096
    assert (record.url, record.links, record.updated) == ("", ["https://example.invalid/agenda"], "")
    assert record.aliases == ["calendar:team%20calendar/plan"]


def test_calendar_keeps_a_large_invitee_list_within_the_link_bound(provider: Provider) -> None:
    # 1,001 invitees plus the organizer and an attachment exceed BF's 1,000 links, and so does each list of
    # refs a `cardinality: many` identity field may map: the record still collects.
    attendees = [{"email": f"guest{index:04d}@corp.test"} for index in range(1001)]
    event = {
        "id": "allhands",
        "summary": "All hands",
        "start": {"dateTime": "2026-09-01T10:00:00Z"},
        "organizer": {"email": "ceo@corp.test"},
        "attendees": attendees,
        "attachments": [{"fileUrl": "https://example.invalid/agenda", "title": "Agenda"}],
    }
    provider.install("gws", [{"match": ["events"], "stdout": {"kind": "calendar#events", "items": [event]}}])
    [record] = provider.records("google-calendar.py", "team", START, END)
    assert len(record.links) == 1000
    assert {"person:email/ceo@corp.test", "https://example.invalid/agenda"} <= set(record.links)
    assert record.attributes["participants_truncated"] is True
    counts = {
        key: len(cast(list, record.attributes[key])) for key in ("participants", "participant_refs", "attendee_refs")
    }
    assert counts == {"participants": 1002, "participant_refs": 1000, "attendee_refs": 1000}


def test_drive_snapshot_rejects_unknown_responses_and_accepts_verified_empty(provider: Provider) -> None:
    for payload in ({}, {"error": {"code": 403, "message": "private provider error"}}, {"kind": "other", "files": []}):
        provider.install("gws", [{"match": ["drive"], "stdout": payload}])
        reply = provider.run("google-drive-folders.py")
        assert reply.returncode == 1
        assert not reply.stdout
        assert "private provider error" not in reply.stderr
    provider.install("gws", [{"match": ["drive"], "stdout": {"kind": "drive#fileList", "files": []}}])
    assert provider.records("google-drive-folders.py") == []


@pytest.mark.parametrize("token", [None, False, 0, [], {}])
@pytest.mark.parametrize("adapter", ["google-calendar.py", "google-drive-folders.py", "gmail-headers.py"])
def test_catalogs_reject_non_string_continuations(provider: Provider, adapter: str, token: object) -> None:
    page, arguments = {
        "google-calendar.py": ({"kind": "calendar#events", "items": []}, ["primary", START, END]),
        "google-drive-folders.py": ({"kind": "drive#fileList", "files": []}, []),
        "gmail-headers.py": ({"messages": []}, [START, END]),
    }[adapter]
    provider.install("gws", [{"match": [], "stdout": {**page, "nextPageToken": token}}])
    result = provider.run(adapter, *arguments)
    assert result.returncode == 1
    assert not result.stdout
    assert "collection failed" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "items",
    [
        {},
        [None],
        [{"id": ""}],
        [{"id": "a"}, {"id": "a"}],
        [{"id": "a", "start": "invalid"}],
        [{"id": "a", "end": []}],
        [{"id": "a", "source": []}],
        [{"id": "a", "organizer": []}],
        [{"id": "a", "attachments": {}}],
        [{"id": "a", "attachments": [None]}],
    ],
)
def test_calendar_rejects_invalid_event_lists(provider: Provider, items: object) -> None:
    provider.install("gws", [{"match": [], "stdout": {"kind": "calendar#events", "items": items}}])
    result = provider.run("google-calendar.py", "primary", START, END)
    assert result.returncode == 1
    assert not result.stdout
    assert "collection failed" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("adapter", ["google-calendar.py", "google-drive-folders.py"])
def test_incomplete_provider_snapshot_preserves_saved_records(
    provider: Provider, brain: Store, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    script = Path(__file__).parents[1] / "examples/sensors" / adapter
    calendar = adapter == "google-calendar.py"
    brain.write("sensors/_sensor.py", (script.parent / "_sensor.py").read_bytes())
    brain.write("sensors/demo.py", script.read_bytes())
    command = ["python3", "sensors/demo.py", *(["primary", "{{start}}", "{{end}}"] if calendar else [])]
    brain.write(
        "bf.yaml",
        yaml.safe_dump(
            {"version": 8, "name": "fixture", "sensors": {"demo": {"mode": "snapshot", "command": command}}}
        ).encode(),
    )
    monkeypatch.setenv(
        "PATH", f"{provider.bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    )
    kind, key = ("calendar#events", "items") if calendar else ("drive#fileList", "files")
    items = [
        {"id": value, "summary": value} if calendar else {"id": value, "name": value, "mimeType": FOLDER}
        for value in ("one", "two")
    ]
    page = {"kind": kind, key: items}
    provider.install("gws", [{"match": [], "stdout": page}])
    runner = CliRunner()
    args = ["collect", "demo", "--brain", str(brain.root)]
    success = runner.invoke(app, args)
    assert success.exit_code == 0, success.output
    saved = {p.name: p.read_bytes() for p in (brain.root / "memories/demo").glob("*.json")}
    assert len(saved) == 2
    provider.install("gws", [{"match": [], "stdout": {"kind": kind, key: items[:1], "nextPageToken": 0}}])
    failure = runner.invoke(app, args)
    assert failure.exit_code == 1
    assert saved == {p.name: p.read_bytes() for p in (brain.root / "memories/demo").glob("*.json")}


# Each updated example with one fake provider reply: the program, its reply, its arguments and its typed claims.
VOCABULARY: dict[str, tuple[str, list[dict[str, object]], list[str], set[tuple[str, str]]]] = {
    "google-drive-folders.py": (
        "gws",
        [
            {
                "match": ["drive"],
                "stdout": {
                    "kind": "drive#fileList",
                    "files": [{"id": "child", "name": "Research", "mimeType": FOLDER, "parents": ["root"]}],
                },
            }
        ],
        [],
        {("part-of", "drive:root")},
    ),
    "gmail-headers.py": (
        "gws",
        [
            {"match": ["list"], "stdout": {"messages": [{"id": "a1"}]}},
            {
                "match": ["get"],
                "stdout": {
                    "id": "a1",
                    "threadId": "f1",
                    "labelIds": ["INBOX", "Label_12"],
                    "internalDate": "1788256800000",
                    "payload": {"headers": [{"name": "From", "value": "alice@example.test"}]},
                },
            },
        ],
        ["{{start}}", "{{end}}"],
        {("part-of", "gmail-thread:f1"), ("label", "gmail-label:INBOX"), ("label", "gmail-label:Label_12")},
    ),
    "github-issues.py": (
        "gh",
        [
            {
                "match": ["issue", "list"],
                "stdout": [
                    {
                        "number": 7,
                        "title": "Plan the launch",
                        "state": "OPEN",
                        "labels": [{"name": "Good First Issue"}, {"name": "bug"}],
                        "createdAt": "2026-09-01T09:00:00Z",
                        "updatedAt": "2026-09-01T10:00:00Z",
                    }
                ],
            }
        ],
        ["Acme/Widgets"],
        {
            ("label", "label:github.com/acme/widgets/good%20first%20issue"),
            ("label", "label:github.com/acme/widgets/bug"),
        },
    ),
}


@pytest.mark.parametrize("adapter", list(VOCABULARY))
def test_new_brains_type_parents_and_labels_and_older_brains_keep_their_links(
    provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    program, responses, arguments, typed = VOCABULARY[adapter]
    monkeypatch.setenv("PATH", f"{provider.bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin")
    runner, claims = CliRunner(), {}
    for kind in ("new", "older"):
        brain = tmp_path / kind
        assert runner.invoke(app, ["init", str(brain), "--name", kind]).exit_code == 0
        config = yaml.safe_load((brain / "bf.yaml").read_text())
        if kind == "older":
            # A brain created before the standard vocabulary declares no fields.
            del config["fields"]
        config["sensors"] = {"source": {"command": ["python3", f"sensors/{adapter}", *arguments]}}
        (brain / "bf.yaml").write_text(yaml.safe_dump(config))
        (brain / "sensors").mkdir()
        for name in (adapter, "_sensor.py"):
            (brain / "sensors" / name).write_bytes((ROOT / "examples/sensors" / name).read_bytes())
        provider.install(program, responses)
        collected = runner.invoke(app, ["collect", "source", "--brain", str(brain), "--since", START, "--until", END])
        assert collected.exit_code == 0, collected.output
        assert runner.invoke(app, ["validate", "--brain", str(brain)]).exit_code == 0
        exported = runner.invoke(app, ["export", "--brain", str(brain)])
        assert exported.exit_code == 0, exported.output
        lines = [json.loads(line) for line in exported.stdout.splitlines()]
        claims[kind] = {
            (line["relation"], line["target"]) for line in lines if line["subject"].startswith(f"bf://{kind}/source:")
        }
    assert {claim for claim in claims["new"] if claim[0] in {"part-of", "label"}} == typed
    # Without the fields, the same records keep exactly their untyped links, as before the vocabulary grew.
    assert claims["older"] == {claim for claim in claims["new"] if claim[0] == "links"}
    assert claims["older"]
