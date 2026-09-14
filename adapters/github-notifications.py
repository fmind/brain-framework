#!/usr/bin/env python3
"""Collect GitHub notifications updated in one window through the owner's gh CLI."""

import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime

MAX_BYTES = 16 << 20
MAX_PAGES = 50
MAX_RECORDS = 10_000
HTML = re.compile(
    r"^https://api\.github\.com/repos/(?P<repo>[^/]+/[^/]+)/(?P<kind>pulls|issues|commits)/(?P<key>[^/]+)$"
)
KINDS = {"pulls": "pull", "issues": "issues", "commits": "commit"}


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamps require a timezone")
    return parsed.astimezone(UTC)


def page(since: str, before: str, number: int) -> tuple[bool, list[dict[str, object]]]:
    # https://docs.github.com/en/rest/activity/notifications#list-notifications-for-the-authenticated-user
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "--include",
                "/notifications",
                "-F",
                "all=true",
                "-f",
                f"since={since}",
                "-f",
                f"before={before}",
                "-F",
                "per_page=100",
                "-F",
                f"page={number}",
            ],
            check=True,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        output.seek(0)
        payload = output.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("notification page exceeds 16 MiB")
    text = payload.decode()
    separator = re.search(r"\r?\n[ \t]*\r?\n", text)
    if separator is None:
        raise ValueError("notification page has no headers")
    headers = text[: separator.start()].splitlines()
    has_next = any(line.lower().startswith("link:") and 'rel="next"' in line for line in headers)
    items = json.loads(text[separator.end() :])
    if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, dict) for item in items):
        raise ValueError("unexpected notification page shape")
    return has_next, items


def html_url(value: object) -> str:
    """Derive the browser URL when the subject is an issue, pull request or commit; keep other API URLs."""
    if not isinstance(value, str):
        return ""
    match = HTML.fullmatch(value)
    if match is None:
        return value
    return f"https://github.com/{match['repo']}/{KINDS[match['kind']]}/{match['key']}"


def mapping(value: object) -> dict[str, object]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def record(item: dict[str, object]) -> dict[str, object]:
    subject = mapping(item.get("subject"))
    repository = mapping(item.get("repository"))
    full_name = str(repository.get("full_name") or "")
    kind = str(subject.get("type") or "")
    title = " ".join(str(subject.get("title") or "").split()) or f"{kind or 'Notification'} in {full_name}".strip()
    lines = [
        f"Repository: {full_name}",
        f"Type: {kind}",
        f"Reason: {item.get('reason', '')}",
        f"Unread: {'yes' if item.get('unread') else 'no'}",
        f"Updated: {item['updated_at']}",
    ]
    return {
        "id": str(item["id"]),
        "title": title,
        "time": str(item["updated_at"]),
        "text": "\n".join(lines),
        "url": html_url(subject.get("url")),
        "links": [f"repo:github.com/{full_name}"] if full_name else [],
        "aliases": [],
        "attributes": {
            "reason": item.get("reason"),
            "type": kind,
            "repository": full_name,
            "unread": item.get("unread"),
            "last_read_at": item.get("last_read_at"),
            "subject_url": subject.get("url"),
            "latest_comment_url": subject.get("latest_comment_url"),
        },
    }


def collect(start: str, end: str) -> list[dict[str, object]]:
    first, last = instant(start), instant(end)
    if first >= last:
        raise ValueError("start must be earlier than end")
    since, before = (value.strftime("%Y-%m-%dT%H:%M:%SZ") for value in (first, last))
    records: list[dict[str, object]] = []
    previous: list[str] = []
    for number in range(1, MAX_PAGES + 1):
        has_next, items = page(since, before, number)
        identities = [str(item.get("id")) for item in items]
        if items and identities == previous:
            raise ValueError("notification pagination repeated a page")
        previous = identities
        # The provider window is advisory; the exact half-open window is applied here.
        records.extend(record(item) for item in items if first <= instant(str(item["updated_at"])) < last)
        if len(records) > MAX_RECORDS:
            raise ValueError("notifications exceed the record ceiling")
        if not has_next:
            return records
    raise ValueError("notification pagination exceeded 50 pages")


if __name__ == "__main__":
    try:
        value = collect(sys.argv[1], sys.argv[2])
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            raise ValueError("normalized notifications exceed 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError:
        print(
            "GitHub notification collection failed; check gh authentication and the requested window.", file=sys.stderr
        )
        sys.exit(1)
