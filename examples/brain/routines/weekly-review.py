#!/usr/bin/env python3
"""Generate an OKF weekly-review action Markdown summary from pure JSON memories."""

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
    today = datetime.now(UTC).date().isoformat()
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
            else:
                records += 1

    task_lines = "\n".join(open_tasks) if open_tasks else "- None."
    report = (
        f"---\ntype: action\nstatus: draft\nupdated: {today}\n"
        f"summary: Weekly summary of compiled notes, sensor records, and open tasks.\n---\n\n"
        f"# Weekly review\n\n"
        f"## Summary {{#summary}}\n\n"
        f"- Compiled notes: {notes}\n"
        f"- Sensor records: {records}\n"
        f"- Open tasks: {len(open_tasks)}\n\n"
        f"## Open tasks {{#open-tasks}}\n\n"
        f"{task_lines}\n"
    )
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
