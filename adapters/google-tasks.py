#!/usr/bin/env python3
"""Collect every Google Tasks list's tasks updated in a half-open window through the owner's gws CLI."""

import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime

PAGE_BYTES = 16 << 20
PAGE_LIMIT = 20
FIELDS = ("status", "due", "completed", "deleted", "hidden", "parent", "position", "updated")


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return parsed.astimezone(UTC)


def call(arguments: list[str]) -> dict[str, object]:
    with tempfile.TemporaryFile() as output:
        subprocess.run(["gws", *arguments], check=True, stdout=output, stderr=subprocess.DEVNULL, timeout=120)
        output.seek(0)
        payload = output.read(PAGE_BYTES + 1)
    if len(payload) > PAGE_BYTES:
        raise ValueError("provider page exceeds 16 MiB")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("unexpected provider response")
    return value


def pages(command: list[str], params: dict[str, object]) -> list[dict[str, object]]:
    """Follow nextPageToken within a finite ceiling and collect every `items` element."""
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    token = ""
    for _ in range(PAGE_LIMIT):
        page = call([*command, "--params", json.dumps({**params, "pageToken": token} if token else params)])
        found = page.get("items", [])
        if not isinstance(found, list) or any(not isinstance(item, dict) for item in found):
            raise ValueError("unexpected item listing")
        items.extend(found)
        token = str(page.get("nextPageToken") or "")
        if not token:
            return items
        if token in seen:
            raise ValueError("pagination repeated a token")
        seen.add(token)
    raise ValueError("pagination exceeded its page limit")


def collect(start: str, end: str) -> list[dict[str, object]]:
    begin, finish = instant(start), instant(end)
    if begin >= finish:
        raise ValueError("start must be earlier than end")
    # https://developers.google.com/workspace/tasks/reference/rest/v1/tasklists/list
    tasklists = pages(["tasks", "tasklists", "list"], {"maxResults": 100})
    if not tasklists or any(not isinstance(item.get("id"), str) for item in tasklists):
        raise ValueError("the account returned no task list; refusing a complete-looking empty window")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for tasklist in tasklists:
        list_id = str(tasklist["id"])
        list_title = str(tasklist.get("title") or "")
        # https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/list has no upper bound.
        params: dict[str, object] = {
            "tasklist": list_id,
            "maxResults": 100,
            "showCompleted": True,
            "showHidden": True,
            "showDeleted": True,
            "showAssigned": True,
            "updatedMin": begin.isoformat(),
        }
        for task in pages(["tasks", "tasks", "list"], params):
            updated = instant(task.get("updated"))
            if not begin <= updated < finish:
                continue
            task_id = task.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("task without an identifier")
            identifier = f"{list_id}~{task_id}"
            if identifier in seen:
                raise ValueError("task pages returned a duplicate identity")
            seen.add(identifier)
            title = " ".join(str(task.get("title") or "").split()) or "(untitled task)"
            lines = [
                "Task: " + title,
                "List: " + (list_title or list_id),
                "Status: " + str(task.get("status") or "unknown"),
            ]
            for label, key in (("Due", "due"), ("Completed", "completed"), ("Updated", "updated")):
                if task.get(key):
                    lines.append(f"{label}: {task[key]}")
            if task.get("deleted"):
                lines.append("Deleted: true")
            notes = str(task.get("notes") or "").strip()
            if notes:
                lines.append("Notes:\n" + notes)
            records.append(
                {
                    "id": identifier,
                    "title": title,
                    "time": updated.isoformat(),
                    "text": "\n".join(lines),
                    "url": str(task.get("webViewLink") or task.get("selfLink") or ""),
                    "links": [f"tasklist:{list_id}"],
                    "aliases": [f"task:{identifier}"],
                    "attributes": {
                        "list": list_id,
                        "listTitle": list_title,
                        "task": task_id,
                        **{key: task[key] for key in FIELDS if key in task},
                    },
                }
            )
    return records


if __name__ == "__main__":
    try:
        value = collect(sys.argv[1], sys.argv[2])
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > PAGE_BYTES:
            raise ValueError("normalized tasks exceed 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, IndexError:
        print("Tasks collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
