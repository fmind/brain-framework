#!/usr/bin/env python3
"""Collect complete GitHub issue or pull-request searches for one role through the owner's gh CLI."""

import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime

MAX_BYTES = 16 << 20
MAX_PAGES = 10
MAX_SLICES = 64
MAX_RECORDS = 10_000
BODY_CHARS = 16_384
KINDS = {"issues": "is:issue", "prs": "is:pr"}
ROLES = {
    "assignee": "assignee:@me",
    "author": "author:@me",
    "involves": "involves:@me",
    "mentions": "mentions:@me",
    "review-requested": "review-requested:@me",
}
REPOSITORY_URL = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)$")
# A jq projection keeps only reviewed fields in memory; GitHub returns every issue field otherwise.
PROJECTION = (
    "{total_count,incomplete_results,items:[.items[]|{number,title,html_url,state,body,created_at,updated_at,"
    "closed_at,draft,repository_url,user:(.user|if .==null then null else {login} end),"
    "assignees:[.assignees[]?|{login}],labels:[.labels[]?|.name],merged_at:.pull_request.merged_at}]}"
)


def instant(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamps require a timezone")
    return int(parsed.astimezone(UTC).timestamp())


def stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def page(query: str, number: int) -> dict[str, object]:
    # https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "/search/issues",
                "-H",
                "Accept: application/vnd.github+json",
                "-f",
                f"q={query}",
                "-f",
                "sort=updated",
                "-f",
                "order=asc",
                "-F",
                "per_page=100",
                "-F",
                f"page={number}",
                "--jq",
                PROJECTION,
            ],
            check=True,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        output.seek(0)
        payload = output.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("search page exceeds 16 MiB")
    value = json.loads(payload)
    total, incomplete, items = value["total_count"], value["incomplete_results"], value["items"]
    if not isinstance(total, int) or isinstance(total, bool) or total < 0 or incomplete is not False:
        raise ValueError("search page cannot prove completeness")
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("unexpected search page shape")
    return value


def strings(value: object, key: str = "") -> list[str]:
    """Reviewed string projections of a provider list, optionally through one mapping key."""
    if not isinstance(value, list):
        return []
    if key:
        return [str(entry[key]) for entry in value if isinstance(entry, dict) and entry.get(key) is not None]
    return [str(entry) for entry in value if entry is not None]


def record(item: dict[str, object]) -> dict[str, object]:
    match = REPOSITORY_URL.search(str(item["repository_url"]))
    if match is None:
        raise ValueError("search item has no repository")
    repository = match["repo"]
    number = item["number"]
    if not isinstance(number, int) or isinstance(number, bool):
        raise ValueError("search item has no number")
    user = item.get("user")
    author = str(user["login"]) if isinstance(user, dict) else ""
    assignees = strings(item.get("assignees"), "login")
    labels = strings(item.get("labels"))
    title = " ".join(str(item["title"]).split()) or f"{repository}#{number}"
    lines = [
        f"Repository: {repository}",
        f"State: {item.get('state', '')}",
        f"Author: {author}",
        f"Assignees: {', '.join(assignees)}",
        f"Labels: {', '.join(labels)}",
        f"Updated: {item['updated_at']}",
        f"Closed: {item.get('closed_at') or ''}",
    ]
    body = item.get("body") or ""
    if body:
        lines.append("")
        lines.append(str(body)[:BODY_CHARS])
    return {
        "id": str(item["html_url"]),
        "title": f"{repository}#{number}: {title}",
        "time": str(item["updated_at"]),
        "text": "\n".join(lines),
        "url": str(item["html_url"]),
        "links": [f"repo:github.com/{repository}"],
        "aliases": [f"github:{repository}#{number}"],
        "attributes": {
            "number": number,
            "state": item.get("state"),
            "draft": item.get("draft"),
            "merged_at": item.get("merged_at"),
            "labels": labels,
            "assignees": assignees,
            "author": author,
            "created_at": item.get("created_at"),
            "closed_at": item.get("closed_at"),
        },
    }


def collect(start: int, end: int, qualifiers: str, slices: list[int]) -> list[dict[str, object]]:
    """Return every result in [start, end); split a saturated window because GitHub caps a search at 1,000."""
    slices[0] += 1
    if slices[0] > MAX_SLICES:
        raise ValueError("search slice ceiling reached")
    query = f"{qualifiers} updated:{stamp(start)}..{stamp(end - 1)}"
    items: list[dict[str, object]] = []
    totals: set[int] = set()
    for number in range(1, MAX_PAGES + 1):
        value = page(query, number)
        total = value["total_count"]
        totals.add(int(str(total)))
        if int(str(total)) >= 1000:
            if end - start <= 1:
                raise ValueError("one-second slice is saturated")
            middle = start + (end - start) // 2
            return [*collect(start, middle, qualifiers, slices), *collect(middle, end, qualifiers, slices)]
        page_items = value["items"]
        if not isinstance(page_items, list):
            raise ValueError("unexpected search page shape")
        items.extend(page_items)
        if len(items) >= int(str(total)) or not page_items:
            break
    if len(totals) != 1:
        raise ValueError("search total changed between pages")
    total = totals.pop()
    urls = {str(item["html_url"]) for item in items}
    if len(items) != total or len(urls) != total:
        raise ValueError("search results are incomplete")
    if len(items) > MAX_RECORDS:
        raise ValueError("search results exceed the record ceiling")
    return [record(item) for item in items]


if __name__ == "__main__":
    try:
        kind, role, start, end = sys.argv[1:5]
        qualifiers = f"{KINDS[kind]} {ROLES[role]}"
        first, last = instant(start), instant(end)
        if first >= last:
            raise ValueError("start must be earlier than end")
        results = collect(first, last, qualifiers, [0])
        unique = {str(item["id"]): item for item in results}
        if len(unique) > MAX_RECORDS:
            raise ValueError("search results exceed the record ceiling")
        payload = json.dumps(sorted(unique.values(), key=lambda item: str(item["id"])), ensure_ascii=False)
        if len(payload.encode()) > MAX_BYTES:
            raise ValueError("normalized search exceeds 16 MiB")
        print(payload)
    except (
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        print(
            "GitHub search collection failed; usage is github-search.py <issues|prs> "
            "<assignee|author|involves|mentions|review-requested> START END with gh logged in.",
            file=sys.stderr,
        )
        sys.exit(1)
