"""The Gmail headers sensor saves who wrote to whom, when and about what: never a message body."""

from __future__ import annotations

import json

import pytest

from conftest import Provider

START, END = "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00"


def message(identifier: str, thread: str, millis: int, headers: dict[str, str]) -> dict[str, object]:
    return {
        "id": identifier,
        "threadId": thread,
        "labelIds": ["INBOX", "IMPORTANT"],
        "internalDate": str(millis),
        "payload": {"headers": [{"name": name, "value": value} for name, value in headers.items()]},
    }


def install(provider: Provider, *, failing: bool = False, repeated: bool = False) -> None:
    first = {"messages": [{"id": "a1"}, {"id": "b2"}], "nextPageToken": "next"}
    second = {"messages": [{"id": "c3"}], **({"nextPageToken": "next"} if repeated else {})}
    provider.install(
        "gws",
        [
            {"match": ["list", "-category:promotions"], "stdout": first},
            {"match": ["list", '"pageToken": "next"'], "stdout": second, "code": 1 if failing else 0},
            {
                "match": ["get", '"id": "a1"'],
                "stdout": message(
                    "a1",
                    "f1",
                    1788256800000,
                    {
                        "From": "Alice <Alice@Example.test>",
                        "To": "bob@example.test, Carol <carol@example.test>",
                        "Subject": "Launch\tbudget",
                        "Message-ID": "<launch.1@example.test>",
                    },
                ),
            },
            {
                "match": ["get", '"id": "b2"'],
                "stdout": message("b2", "f1", 1788256900000, {"From": "bob@example.test", "Subject": ""}),
            },
            # Gmail's after:/before: round to seconds: a message outside the window is dropped, not saved.
            {"match": ["get", '"id": "c3"'], "stdout": message("c3", "f2", 1788307200000, {"From": "x@example.test"})},
        ],
    )


def test_gmail_headers_collect_participants_threads_and_message_ids(provider: Provider) -> None:
    install(provider)
    records = provider.records("gmail-headers.py", START, END, "--query=-category:promotions")
    assert [record.id for record in records] == ["a1", "b2"]
    launch, reply = records
    assert (launch.title, launch.time) == ("Launch budget", "2026-09-01T10:00:00.000000Z")
    assert launch.text == (
        "Subject: Launch budget\nFrom: alice@example.test\nTo: bob@example.test, carol@example.test\n"
        "Labels: IMPORTANT, INBOX"
    )
    assert launch.links == [
        "gmail-thread:f1",
        "person:email/alice@example.test",
        "person:email/bob@example.test",
        "person:email/carol@example.test",
    ]
    assert launch.aliases == ["mid:launch.1@example.test"]
    assert launch.url == "https://mail.google.com/mail/#all/f1"
    # Both messages link their thread: reading gmail-thread:f1 lists the conversation.
    assert "gmail-thread:f1" in reply.links
    assert reply.title == "(no subject)"
    # Only metadata is requested: Gmail never sends a body.
    gets = [json.loads(call[-1]) for call in provider.calls("gws") if "get" in call]
    assert {params["format"] for params in gets} == {"metadata"}


def test_gmail_headers_widen_whole_second_bounds_and_filter_exactly(provider: Provider) -> None:
    # Gmail's after: and before: take whole seconds and after: excludes its own second. Truncating both bounds asked
    # for after:1788220800 before:1788256800 and missed messages in the window's first and last fractions of a second.
    start, end = "2026-09-01T00:00:00.500000+00:00", "2026-09-01T10:00:00.250000+00:00"
    received = {"a1": 1788220800499, "b2": 1788220800500, "c3": 1788256800249, "d4": 1788256800250}
    provider.install(
        "gws",
        [
            {
                "match": ["list", "after:1788220799 before:1788256801"],
                "stdout": {"messages": [{"id": i} for i in received]},
            },
            *(
                {"match": ["get", f'"id": "{identifier}"'], "stdout": message(identifier, "f1", millis, {})}
                for identifier, millis in received.items()
            ),
        ],
    )
    records = provider.records("gmail-headers.py", start, end)
    # The window is half-open: a message received at START counts, one at END belongs to the next window.
    assert [(record.id, record.time) for record in records] == [
        ("b2", "2026-09-01T00:00:00.500000Z"),
        ("c3", "2026-09-01T10:00:00.249000Z"),
    ]


@pytest.mark.parametrize("problem", ["failing", "repeated"])
def test_gmail_headers_save_nothing_from_an_incomplete_listing(provider: Provider, problem: str) -> None:
    install(provider, failing=problem == "failing", repeated=problem == "repeated")
    result = provider.run("gmail-headers.py", START, END, "--query=-category:promotions")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr.startswith("Gmail collection failed")
    assert "example.test" not in result.stderr
