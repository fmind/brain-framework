#!/usr/bin/env python3
"""Generate a weekly-review summary memory record from pure JSON memories."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

AUTHORED = {"projects", "concepts", "actions"}


def main() -> int:
    root = Path(os.environ.get("BF_BRAIN", ".")).resolve()
    memories = root / "memories"
    now = datetime.now(UTC)
    today = now.date().isoformat()
    notes = records = 0
    open_tasks: list[str] = []

    if memories.is_dir() and not memories.is_symlink():
        for path in sorted(memories.rglob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(memories).parts
            if not rel:
                continue
            doc = json.loads(path.read_bytes())
            if not isinstance(doc, dict):
                continue
            if rel[0] in AUTHORED:
                notes += 1
                ref = str(doc.get("ref") or doc.get("id") or path.stem)
                open_tasks.extend(
                    f"- `{ref}`: {task['text']}"
                    for task in (doc.get("tasks") or ())
                    if isinstance(task, dict) and not task.get("done") and task.get("text")
                )
            elif rel[0] != "weekly-review":
                records += 1

    task_lines = "\n".join(open_tasks) if open_tasks else "- None."
    body = (
        f"## Summary\n\n"
        f"- Compiled notes: {notes}\n"
        f"- Sensor records: {records}\n"
        f"- Open tasks: {len(open_tasks)}\n\n"
        f"## Open tasks\n\n"
        f"{task_lines}\n"
    )
    record = {
        "id": f"weekly-review-{today}",
        "title": f"Weekly review ({today})",
        "summary": f"{notes} notes, {records} records, {len(open_tasks)} open tasks.",
        "time": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "text": body,
        "attributes": {"notes": notes, "records": records, "open_tasks": len(open_tasks)},
    }
    sys.stdout.write(json.dumps([record], ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
