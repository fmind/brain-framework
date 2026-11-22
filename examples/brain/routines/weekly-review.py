#!/usr/bin/env python3
"""Generate a weekly-review summary memory record from JSONL memories."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    root = Path(os.environ.get("BF_BRAIN", ".")).resolve()
    memories = root / "memories"
    now = datetime.now(UTC)
    today = now.date().isoformat()
    notes = records = 0
    open_tasks: list[str] = []

    if memories.is_dir() and not memories.is_symlink():
        for path in sorted(memories.glob("*.jsonl")):
            if path.is_symlink() or not path.is_file():
                continue
            source = path.stem
            for raw_line in path.read_bytes().splitlines():
                if not (stripped := raw_line.strip()):
                    continue
                doc = json.loads(stripped.decode("utf-8"))
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") == "note":
                    notes += 1
                    ref = str(doc.get("ref") or doc.get("id") or source)
                    open_tasks.extend(
                        f"- `{ref}`: {task['text']}"
                        for task in (doc.get("tasks") or ())
                        if isinstance(task, dict) and not task.get("done") and task.get("text")
                    )
                elif source != "weekly-review":
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
