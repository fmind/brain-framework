"""Gmail, Tasks and Contacts adapters project facts from fake gws pages and fail closed."""

from __future__ import annotations

from conftest import Provider

START, END = "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"
ADDRESS = "colleague@example.invalid"
FAILURE = {"match": ["gws"], "stdout": "", "stderr": f"token expired for {ADDRESS}", "code": 1, "repeat": True}


def message(identifier: str, internal: str, headers: dict[str, str], snippet: str = "") -> dict[str, object]:
    return {
        "id": identifier,
        "threadId": "t-" + identifier,
        "internalDate": internal,
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": snippet,
        "sizeEstimate": 1234,
        "payload": {"headers": [{"name": name, "value": value} for name, value in headers.items()]},
    }


def test_gmail_pages_filters_window_and_projects_headers(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {"match": ["messages", "list", '"pageToken": "second"'], "stdout": {"messages": [{"id": "m3"}]}},
            {
                "match": ["messages", "list"],
                "stdout": {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "second"},
            },
            {
                "match": ["messages", "get", '"id": "m1"'],
                "stdout": message(
                    "m1",
                    "1788260400000",
                    {
                        "From": "Médéric <owner@example.invalid>",
                        "To": f"Colleague <{ADDRESS}>, other@example.invalid",
                        "Cc": "=?UTF-8?B?w4lxdWlwZQ==?= <team@example.invalid>",
                        "Subject": "=?UTF-8?Q?R=C3=A9union_de_suivi?=\t urgent",
                        "Date": "Mon, 1 Sep 2026 12:00:00 +0200",
                    },
                    "Ordre du jour: décisions",
                ),
            },
            {"match": ["messages", "get", '"id": "m2"'], "stdout": message("m2", "1788393600000", {"Subject": "x"})},
            {"match": ["messages", "get", '"id": "m3"'], "stdout": message("m3", "1788260401000", {})},
        ],
    )
    records = provider.records("google-gmail.py", START, END, "in:anywhere")
    assert [r.id for r in records] == ["m1", "m3"]
    mail = records[0]
    assert mail.title == "Réunion de suivi urgent"
    assert mail.time == "2026-09-01T11:00:00.000000Z"
    for fact in ("From: Médéric <owner@example.invalid>", "Cc: Équipe <team@example.invalid>", "Labels: INBOX, UNREAD"):
        assert fact in mail.text
    assert mail.text.endswith("Ordre du jour: décisions")
    assert mail.url == "https://mail.google.com/mail/u/0/#all/m1"
    assert mail.links == [
        f"person:email/{ADDRESS}",
        "person:email/other@example.invalid",
        "person:email/owner@example.invalid",
        "person:email/team@example.invalid",
    ]
    assert mail.aliases == ["mail:m1"]
    assert mail.attributes["threadId"] == "t-m1"
    assert records[1].title == "(no subject)"
    calls = provider.calls("gws")
    assert len(calls) == 5
    assert '"q": "after:1788220799 before:1788307200 in:anywhere"' in calls[0][-1]


def test_gmail_fails_closed(provider: Provider) -> None:
    for responses in [
        [FAILURE],
        [{"match": ["messages", "list"], "stdout": {"messages": [], "nextPageToken": "loop"}, "repeat": True}],
        [{"match": ["messages", "list"], "stdout": {"messages": "broken"}}],
        [
            {"match": ["messages", "list"], "stdout": {"messages": [{"id": "m1"}]}},
            {"match": ["messages", "get"], "stdout": {"id": "m1", "internalDate": "1788260400000", "payload": {}}},
        ],
    ]:
        provider.install("gws", responses)
        result = provider.run("google-gmail.py", START, END)
        assert result.returncode == 1
        assert result.stdout == ""
        assert ADDRESS not in result.stderr
        assert "Gmail collection failed" in result.stderr
    assert provider.run("google-gmail.py", END, START).returncode == 1


def test_tasks_walk_every_list_and_bound_the_window(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {
                "match": ["tasklists", "list", '"pageToken": "more"'],
                "stdout": {"items": [{"id": "L2", "title": "Perso"}]},
            },
            {
                "match": ["tasklists", "list"],
                "stdout": {"items": [{"id": "L1", "title": "Travail"}], "nextPageToken": "more"},
            },
            {
                "match": ["tasks", "list", '"tasklist": "L1"'],
                "stdout": {
                    "items": [
                        {
                            "id": "a",
                            "title": "Préparer  la réunion",
                            "notes": "Apporter le dossier.",
                            "status": "needsAction",
                            "due": "2026-09-03T00:00:00.000Z",
                            "updated": "2026-09-01T09:30:00.000Z",
                            "webViewLink": "https://tasks.google.com/task/a",
                            "position": "00000000000000000001",
                        },
                        {
                            "id": "late",
                            "title": "Too new",
                            "status": "needsAction",
                            "updated": "2026-09-02T00:00:00.000Z",
                        },
                    ]
                },
            },
            {
                "match": ["tasks", "list", '"tasklist": "L2"'],
                "stdout": {
                    "items": [
                        {
                            "id": "b",
                            "title": "",
                            "status": "completed",
                            "completed": "2026-09-01T10:00:00.000Z",
                            "updated": "2026-09-01T10:00:00.000Z",
                            "deleted": True,
                        }
                    ]
                },
            },
        ],
    )
    records = provider.records("google-tasks.py", START, END)
    assert [r.id for r in records] == ["L1~a", "L2~b"]
    task = records[0]
    assert task.title == "Préparer la réunion"
    assert task.time == "2026-09-01T09:30:00.000000Z"
    for fact in (
        "List: Travail",
        "Status: needsAction",
        "Due: 2026-09-03T00:00:00.000Z",
        "Notes:\nApporter le dossier.",
    ):
        assert fact in task.text
    assert task.url == "https://tasks.google.com/task/a"
    assert task.links == ["tasklist:L1"]
    assert task.aliases == ["task:L1~a"]
    assert task.attributes["listTitle"] == "Travail"
    assert records[1].title == "(untitled task)"
    assert "Deleted: true" in records[1].text
    assert '"updatedMin": "2026-09-01T00:00:00+00:00"' in provider.calls("gws")[2][-1]


def test_tasks_fail_closed(provider: Provider) -> None:
    for responses in [
        [FAILURE],
        [{"match": ["tasklists", "list"], "stdout": {"items": []}}],
        [
            {
                "match": ["tasklists", "list"],
                "stdout": {"items": [{"id": "L1"}], "nextPageToken": "loop"},
                "repeat": True,
            }
        ],
        [
            {"match": ["tasklists", "list"], "stdout": {"items": [{"id": "L1"}]}},
            {"match": ["tasks", "list"], "stdout": {"items": [{"id": "a", "updated": "2026-09-01"}]}},
        ],
        [
            {"match": ["tasklists", "list"], "stdout": {"items": [{"id": "L1"}]}},
            {"match": ["tasks", "list"], "stdout": {"items": "broken"}},
        ],
    ]:
        provider.install("gws", responses)
        result = provider.run("google-tasks.py", START, END)
        assert result.returncode == 1
        assert result.stdout == ""
        assert ADDRESS not in result.stderr
        assert "Tasks collection failed" in result.stderr


def test_contacts_project_names_emails_and_organizations(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {
                "match": ["connections", "list", '"pageToken": "next"'],
                "stdout": {
                    "connections": [
                        {"resourceName": "people/c2", "emailAddresses": [{"value": "Solo@Example.invalid"}]}
                    ]
                },
            },
            {
                "match": ["connections", "list"],
                "stdout": {
                    "connections": [
                        {
                            "resourceName": "people/c1",
                            "names": [{"displayName": "Émilie  Dupont"}],
                            "emailAddresses": [{"value": ADDRESS, "type": "work"}, {"value": "not-an-address"}],
                            "organizations": [{"name": "Example SA", "title": "CTO"}],
                            "phoneNumbers": [{"value": "+352 000"}],
                        },
                        {"resourceName": "people/c3"},
                    ],
                    "nextPageToken": "next",
                },
            },
        ],
    )
    records = provider.records("google-contacts.py", START, END)
    assert [r.id for r in records] == ["people/c1", "people/c3", "people/c2"]
    contact = records[0]
    assert contact.title == "Émilie Dupont"
    assert contact.time == ""
    for fact in (f"Email: {ADDRESS}", "Organization: Example SA", "Title: CTO", "Phone: +352 000"):
        assert fact in contact.text
    assert contact.links == [f"person:email/{ADDRESS}"]
    assert contact.attributes["organizations"] == [{"name": "Example SA", "title": "CTO"}]
    assert records[1].title == "people/c3"
    assert records[2].title == "solo@example.invalid"
    assert '"personFields": "names,emailAddresses,organizations,phoneNumbers"' in provider.calls("gws")[0][-1]


def test_contacts_fail_closed(provider: Provider) -> None:
    for responses in [
        [FAILURE],
        [{"match": ["connections"], "stdout": {"connections": [], "nextPageToken": "loop"}, "repeat": True}],
        [{"match": ["connections"], "stdout": {"connections": [{"names": []}]}}],
        [{"match": ["connections"], "stdout": []}],
    ]:
        provider.install("gws", responses)
        result = provider.run("google-contacts.py", START, END)
        assert result.returncode == 1
        assert result.stdout == ""
        assert ADDRESS not in result.stderr
        assert "Contacts collection failed" in result.stderr
    assert provider.run("google-contacts.py", START).returncode == 1
