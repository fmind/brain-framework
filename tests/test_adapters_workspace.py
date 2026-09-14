"""Meeting-notes and Chat adapters project facts, page completely and fail closed against fake gws."""

from __future__ import annotations

import json

from conftest import Provider

START, END = "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"


def document(paragraphs: list[str]) -> dict[str, object]:
    """The Docs API nesting: body.content[].paragraph.elements[].textRun.content."""
    return {
        "documentId": "x",
        "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": text}}]}} for text in paragraphs]},
    }


def drive_file(identifier: str, name: str, created: str, modified: str) -> dict[str, object]:
    return {
        "id": identifier,
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
        "size": "1234",
        "createdTime": created,
        "modifiedTime": modified,
        "webViewLink": f"https://docs.google.com/document/d/{identifier}/edit",
        "parents": ["folder1"],
        "owners": [{"emailAddress": "Owner@Example.invalid"}],
    }


NOTES_PAGE_ONE = {
    "nextPageToken": "second",
    "files": [
        drive_file("doc1", "Réunion IMA - Notes by Gemini", "2026-09-01T14:01:49.430Z", "2026-09-01T15:00:00Z"),
        drive_file("old", "Old - Notes by Gemini", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
    ],
}
NOTES_PAGE_TWO = {
    "files": [drive_file("doc3", "Long - Notes by Gemini", "2026-08-20T00:00:00Z", "2026-09-01T09:00:00Z")]
}


def test_meeting_notes_project_exported_text_owners_and_window(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {"match": ["drive", "files", "list", '"pageToken": "second"'], "stdout": NOTES_PAGE_TWO},
            {"match": ["drive", "files", "list"], "stdout": NOTES_PAGE_ONE},
            {
                "match": ["docs", "documents", "get", "doc1"],
                "stdout": document(["Décision: garder les originaux.\n", "Fin."]),
            },
            {"match": ["docs", "documents", "get", "doc3"], "stdout": document(["x" * 70_000])},
        ],
    )
    records = provider.records("google-meeting-notes.py", START, END)
    assert [record.id for record in records] == ["doc1", "doc3"]
    note = records[0]
    assert note.title == "Réunion IMA - Notes by Gemini"
    assert note.time == "2026-09-01T14:01:49.430000Z"
    assert note.url == "https://docs.google.com/document/d/doc1/edit"
    for fact in (
        "Created: 2026-09-01T14:01:49.430Z",
        "Owners: owner@example.invalid",
        "Décision: garder les originaux.",
    ):
        assert fact in note.text
    assert note.links == ["person:email/owner@example.invalid"]
    assert note.aliases == ["drive:doc1"]
    assert note.attributes["parents"] == ["folder1"]
    assert note.attributes["size"] == "1234"
    assert "[truncated: document text exceeds 64 KiB]" in records[1].text
    assert len(records[1].text.encode()) < 66_000
    calls = provider.calls("gws")
    assert len(calls) == 4
    assert "name contains 'Notes by Gemini'" in calls[0][-1]
    assert "old" not in {call[-1] for call in calls if "docs" in call}


def test_meeting_notes_accept_a_custom_name_and_reject_query_injection(provider: Provider) -> None:
    provider.install("gws", [{"match": ["drive", "files", "list", "Weekly"], "stdout": {"files": []}}])
    assert provider.records("google-meeting-notes.py", START, END, "Weekly") == []
    result = provider.run("google-meeting-notes.py", START, END, "x' or name contains 'secret")
    assert result.returncode == 1
    assert result.stdout == ""
    assert provider.calls("gws") == [provider.calls("gws")[0]]


def test_meeting_notes_fail_closed(provider: Provider) -> None:
    for responses in [
        [{"match": ["drive"], "stdout": "", "stderr": "token expired for user@example.invalid", "code": 1}],
        [{"match": ["drive"], "stdout": {**NOTES_PAGE_ONE, "nextPageToken": "loop"}, "repeat": True}],
        [{"match": ["drive"], "stdout": {"files": "nope"}}],
        [{"match": ["drive"], "stdout": [1, 2]}],
        [{"match": ["drive"], "stdout": NOTES_PAGE_TWO}, {"match": ["docs"], "stdout": "", "code": 3}],
    ]:
        provider.install("gws", responses)
        result = provider.run("google-meeting-notes.py", START, END)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "user@example.invalid" not in result.stderr
        assert "Meeting notes collection failed" in result.stderr


def message(name: str, created: str, text: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "createTime": created,
        "text": text,
        "sender": {"name": "users/123", "type": "HUMAN", "displayName": "Ada"},
        "thread": {"name": name.rsplit("/messages/", 1)[0] + "/threads/t1"},
        **extra,
    }


SPACES_ONE = {"nextPageToken": "more", "spaces": [{"name": "spaces/A", "displayName": "Équipe", "spaceType": "SPACE"}]}
SPACES_TWO = {"spaces": [{"name": "spaces/B", "spaceType": "DIRECT_MESSAGE"}]}
MESSAGES_A_ONE = {
    "nextPageToken": "m2",
    "messages": [
        message(
            "spaces/A/messages/one.one", "2026-09-01T10:00:00.123456Z", "Décision prise:\nconserver les originaux."
        ),
        message("spaces/A/messages/late.late", "2026-09-02T00:00:00Z", "Outside the window."),
    ],
}
MESSAGES_A_TWO = {
    "messages": [message("spaces/A/messages/gone.gone", "2026-09-01T11:00:00Z", "", deleteTime="2026-09-01T12:00:00Z")]
}
MESSAGES_B = {"messages": [{"name": "spaces/B/messages/dm.dm", "createTime": "2026-09-01T08:00:00Z", "text": "ping"}]}


def test_chat_lists_every_space_and_projects_messages(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {"match": ["chat", "spaces", "list", '"pageToken": "more"'], "stdout": SPACES_TWO},
            {"match": ["chat", "spaces", "list"], "stdout": SPACES_ONE},
            {"match": ["messages", "list", "spaces/A", '"pageToken": "m2"'], "stdout": MESSAGES_A_TWO},
            {"match": ["messages", "list", "spaces/A"], "stdout": MESSAGES_A_ONE},
            {"match": ["messages", "list", "spaces/B"], "stdout": MESSAGES_B},
        ],
    )
    records = provider.records("google-chat.py", START, END)
    assert [record.id for record in records] == [
        "spaces/A/messages/one.one",
        "spaces/A/messages/gone.gone",
        "spaces/B/messages/dm.dm",
    ]
    first = records[0]
    assert first.title == "Équipe: Ada - Décision prise: conserver les originaux."
    assert first.time == "2026-09-01T10:00:00.123456Z"
    for fact in ("Space: Équipe (spaces/A)", "Sender: Ada", "Thread: spaces/A/threads/t1", "conserver les originaux."):
        assert fact in first.text
    assert first.links == ["chat:spaces/A"]
    assert first.attributes == {
        "space": "spaces/A",
        "thread": "spaces/A/threads/t1",
        "sender": "users/123",
        "deleted": False,
    }
    assert records[1].title == "Équipe: Ada - [deleted message]"
    assert records[1].attributes["deleted"] is True
    assert records[2].title == "Direct message: unknown sender - ping"
    calls = provider.calls("gws")
    assert len(calls) == 5
    params = json.loads(calls[3][-1])
    assert params["filter"] == 'createTime > "2026-08-31T23:59:59Z" AND createTime < "2026-09-02T00:00:00Z"'
    assert params["showDeleted"] is True
    assert params["parent"] == "spaces/A"


def test_chat_fails_closed(provider: Provider) -> None:
    for responses in [
        [{"match": ["chat"], "stdout": "", "stderr": "denied for user@example.invalid", "code": 1}],
        [{"match": ["chat", "spaces", "list"], "stdout": {**SPACES_ONE, "nextPageToken": "loop"}, "repeat": True}],
        [{"match": ["chat", "spaces", "list"], "stdout": {"spaces": [{"displayName": "no name"}]}}],
        [
            {"match": ["chat", "spaces", "list"], "stdout": SPACES_TWO},
            {"match": ["messages", "list"], "stdout": {"messages": [{"text": "no name or time"}]}},
        ],
        [{"match": ["chat", "spaces", "list"], "stdout": {"spaces": "nope"}}],
    ]:
        provider.install("gws", responses)
        result = provider.run("google-chat.py", START, END)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "user@example.invalid" not in result.stderr
        assert "Chat collection failed" in result.stderr
    assert provider.run("google-chat.py", END, START).returncode == 1
