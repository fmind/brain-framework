"""The complete snapshot example links folders to their parents and fails closed."""

import json

from conftest import Provider

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


def test_drive_snapshot_rejects_unknown_responses_and_accepts_verified_empty(provider: Provider) -> None:
    for payload in ({}, {"error": {"code": 403, "message": "private provider error"}}, {"kind": "other", "files": []}):
        provider.install("gws", [{"match": ["drive"], "stdout": payload}])
        reply = provider.run("google-drive-folders.py")
        assert reply.returncode == 1
        assert not reply.stdout
        assert "private provider error" not in reply.stderr
    provider.install("gws", [{"match": ["drive"], "stdout": {"kind": "drive#fileList", "files": []}}])
    assert provider.records("google-drive-folders.py") == []
