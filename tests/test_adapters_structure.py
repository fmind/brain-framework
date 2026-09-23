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
                "stdout": {"files": [{"id": "child", "name": "Research", "mimeType": FOLDER, "parents": ["root"]}]},
            },
            {
                "match": ["drive"],
                "stdout": {
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
        ("google-drive-folders.py", [{"match": ["drive"], "stdout": {"incompleteSearch": True, "files": []}}]),
        (
            "google-drive-folders.py",
            [{"match": ["drive"], "stdout": {"files": [], "nextPageToken": "loop"}, "repeat": True}],
        ),
        (
            "google-drive-folders.py",
            [
                {
                    "match": ["drive"],
                    "stdout": {"files": [{"id": "bad", "name": "x", "mimeType": FOLDER, "parents": [42]}]},
                }
            ],
        ),
    ]:
        provider.install("gws", responses)
        result = provider.run(adapter, START, END)
        assert result.returncode == 1
        assert not result.stdout
        assert "collection failed" in result.stderr
