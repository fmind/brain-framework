"""Catalogs and task captures preserve bodies, explicit membership and complete source snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path

from conftest import Provider

START, END = "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"
FOLDER = "application/vnd.google-apps.folder"


def test_gmail_labels_preserve_ids_and_names_without_guessing_parents(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {
                "match": ["labels", "list"],
                "stdout": {
                    "labels": [
                        {"id": "Label_1", "name": "Work", "type": "user"},
                        {"id": "Label_2", "name": "Work/Research", "type": "user"},
                    ]
                },
            }
        ],
    )
    records = provider.records("google-gmail-labels.py", START, END)
    assert [r.kind for r in records] == ["container", "container"]
    assert records[1].title == "Work/Research"
    assert records[1].aliases == ["gmail-label:Label_2"]
    assert records[1].parents == []
    assert records[1].attributes["name"] == "Work/Research"
    assert len(provider.calls("gws")) == 1


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
    assert records[1].parents == ["drive:root"]
    assert records[1].kind == "container"
    params = json.loads(provider.calls("gws")[0][-1])
    assert "incompleteSearch" in params["fields"]
    assert params["corpora"] == "user"


def test_catalogs_fail_before_partial_output(provider: Provider) -> None:
    for adapter, responses in [
        ("google-gmail-labels.py", [{"match": ["labels"], "stdout": {"labels": [{"id": "x"}]}}]),
        (
            "google-gmail-labels.py",
            [
                {
                    "match": ["labels"],
                    "stdout": {"labels": [{"id": "x", "name": "One"}, {"id": "x", "name": "Duplicate"}]},
                }
            ],
        ),
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


def bookmarks() -> dict[str, object]:
    return {
        "version": 1,
        "roots": {
            "bookmark_bar": {
                "id": "1",
                "type": "folder",
                "name": "Work",
                "children": [
                    {
                        "id": "2",
                        "type": "folder",
                        "name": "Research",
                        "children": [
                            {"id": "3", "type": "url", "name": "Evidence", "url": "https://example.invalid/evidence"}
                        ],
                    },
                    {"id": "4", "type": "folder", "name": "Research", "children": []},
                ],
            }
        },
    }


def test_chrome_preserves_empty_folders_and_distinct_same_named_folders(provider: Provider, tmp_path: Path) -> None:
    path = tmp_path / "Bookmarks"
    path.write_text(json.dumps(bookmarks()))
    records = provider.records("chrome-bookmarks.py", "personal", str(path), START, END)
    by_id = {r.id: r for r in records}
    assert len(by_id) == 4
    assert by_id["personal/3"].parents == ["bookmark:personal/2"]
    assert by_id["personal/4"].kind == "container"
    assert by_id["personal/2"].aliases != by_id["personal/4"].aliases
    assert "Work / Research" in by_id["personal/3"].text
    assert "children" not in by_id["personal/1"].attributes
    other = provider.records("chrome-bookmarks.py", "team", str(path), START, END)
    assert set(by_id).isdisjoint(r.id for r in other)


def test_chrome_rejects_redirected_inputs_and_invalid_trees(provider: Provider, tmp_path: Path) -> None:
    path = tmp_path / "Bookmarks"
    path.write_text(json.dumps(bookmarks()))
    linked = tmp_path / "linked"
    linked.symlink_to(path)
    assert provider.run("chrome-bookmarks.py", "personal", str(linked)).returncode == 1
    value = json.loads(json.dumps(bookmarks()))
    value["roots"]["bookmark_bar"]["children"].append({"id": "2", "type": "url", "name": "Duplicate", "url": "x"})
    path.write_text(json.dumps(value))
    result = provider.run("chrome-bookmarks.py", "personal", str(path))
    assert result.returncode == 1
    assert not result.stdout


def test_task_capture_preserves_full_body_and_artifact_metadata(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    task = root / "retention"
    (task / "inputs").mkdir(parents=True)
    (task / "outputs").mkdir()
    body = "# Retention review\n\nPreserve source evidence.\n\n- [x] Inspect sources\n- [ ] Review recommendation\n"
    (task / "TASK.md").write_text(body)
    (task / "inputs/request.txt").write_text("Investigate retention")
    (task / "outputs/report.md").write_text("Keep originals")
    for path in task.rglob("*"):
        os.utime(path, (1788260400, 1788260400))
    records = provider.records("task-folders.py", str(root), START, END)
    assert len(records) == 1
    assert records[0].text == body
    assert records[0].aliases == ["task:retention"]
    files = json.loads(json.dumps(records[0].attributes["files"]))
    assert {entry["path"] for entry in files} == {
        "retention/TASK.md",
        "retention/inputs/request.txt",
        "retention/outputs/report.md",
    }
    assert provider.records("task-folders.py", str(root), END, "2026-09-03T00:00:00Z") == []
    (task / "inputs/link").symlink_to(task / "TASK.md")
    result = provider.run("task-folders.py", str(root), START, END)
    assert result.returncode == 1
    assert not result.stdout
