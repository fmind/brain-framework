#!/usr/bin/env python3
"""Collect one GitHub repository's issues updated in the window through the owner's gh CLI, using the sensor kit.

Usage: github-issues.py OWNER/REPO [START END]. BF passes the window in BF_START and BF_END; START and END serve a
manual run. Needs `_sensor.py` in the same folder.
"""

import json
import math
import re
import sys
from datetime import UTC, datetime
from typing import Any

import _sensor

MAX_ISSUES = 500
MAX_BYTES = 16 << 20
TIMEOUT = 60
FIELDS = "number,title,body,state,stateReason,author,assignees,labels,createdAt,updatedAt,closedAt,url"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")


def login(value: object) -> str:
    """A GitHub account's login, or "" for a deleted (ghost) account."""
    name = value.get("login") if isinstance(value, dict) else None
    return name if isinstance(name, str) else ""


def issue(repository: str, item: object, start: datetime, end: datetime) -> dict[str, Any] | None:
    """One issue as a record; None when its last update falls outside the window the search widened."""
    if not isinstance(item, dict):
        raise _sensor.SensorError("gh returned an invalid issue")
    number, title, state = item.get("number"), item.get("title"), item.get("state")
    created, updated = item.get("createdAt"), item.get("updatedAt")
    assignees, labels = item.get("assignees") or [], item.get("labels") or []
    if (
        type(number) is not int
        or number <= 0
        or not isinstance(title, str)
        or state not in ("OPEN", "CLOSED")
        or not isinstance(created, str)
        or not isinstance(updated, str)
        or not isinstance(assignees, list)
        or not isinstance(labels, list)
    ):
        raise _sensor.SensorError("gh returned an invalid issue")
    if not start <= datetime.fromisoformat(updated) < end:
        return None
    owner, name = repository.split("/")
    repository_ref = _sensor.repo("github.com", owner, name)
    author = login(item.get("author"))
    people = [login(person) for person in assignees]
    names = sorted(
        {label["name"] for label in labels if isinstance(label, dict) and isinstance(label.get("name"), str)}
    )
    status = state.lower()
    reason = item.get("stateReason")
    body = item.get("body")
    lines = [f"Repository: {repository}", f"Issue: #{number}", f"Status: {status}"]
    lines += [f"Author: {author}"] if author else []
    lines += [f"Assignees: {', '.join(filter(None, people))}"] if any(people) else []
    lines += [f"Labels: {', '.join(names)}"] if names else []
    author_refs = [_sensor.account("github.com", author)] if author else []
    assignee_refs = [_sensor.account("github.com", person) for person in people if person]
    return _sensor.record(
        f"{repository}/issues/{number}",
        f"{repository}: {title}",
        # Issue bodies sometimes quote a pasted token: mask common credential shapes before saving.
        text="\n".join(lines) + ("\n\n" + _sensor.redact(body) if isinstance(body, str) and body else ""),
        time=created,
        url=item.get("url") or "",
        links=[repository_ref, *author_refs, *assignee_refs],
        aliases=[f"github:{repository}/issues/{number}"],
        attributes={
            "number": number,
            "kind": "issue",
            "status": status,
            **({"reason": reason.lower()} if isinstance(reason, str) and reason else {}),
            "labels": names,
            "updated": updated,
            "author_refs": author_refs,
            "assignee_refs": assignee_refs,
            "repository_refs": [repository_ref],
        },
    )


def collect() -> list[dict[str, Any]]:
    if len(sys.argv) not in (2, 4) or not REPOSITORY.fullmatch(sys.argv[1]):
        raise _sensor.SensorError("usage: github-issues.py OWNER/REPO [START END]")
    repository = sys.argv[1]
    start, end = _sensor.window(sys.argv[2:4])
    # GitHub search ranges are inclusive and take whole seconds: widen outward, then filter exactly.
    first = datetime.fromtimestamp(math.floor(start.timestamp()), UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    last = datetime.fromtimestamp(math.ceil(end.timestamp()), UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    argv = ["gh", "issue", "list", "--repo", repository, "--state", "all", "--search", f"updated:{first}..{last}"]
    # One more than the bound: a full answer means GitHub has more, and a partial catalog must not pass as complete.
    argv += ["--limit", str(MAX_ISSUES + 1), "--json", FIELDS]
    items = json.loads(_sensor.run(argv, timeout=TIMEOUT, max_bytes=MAX_BYTES))
    if not isinstance(items, list):
        raise _sensor.SensorError("gh returned an unexpected issue list")
    if len(items) > MAX_ISSUES:
        raise _sensor.SensorError(f"the window has more than {MAX_ISSUES} updated issues; narrow it")
    found = (issue(repository, item, start, end) for item in items)
    return [record for record in found if record is not None]


if __name__ == "__main__":
    _sensor.main(collect, "GitHub issue collection failed; check gh authentication and the repository")
