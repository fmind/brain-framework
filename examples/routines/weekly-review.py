#!/usr/bin/env python3
"""Weekly review: render the brain's home page and last seven days as a review action; no model involved."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import _bf

REPLY_BYTES = _bf.MAX_REPLY
TIMEOUT = 120
MAX_PROJECT_PAGES = _bf.MAX_PAGES
TASK_PREVIEW = 10
# The home page previews at most 20 changed notes and 20 upcoming items, without a total.
HOME_PREVIEW = 20
# It previews the 10 newest open (`status: draft`) action folders, without a total.
ACTION_PREVIEW = 10
# How the home page's attention entries describe a scheduled program's freshness on this machine.
STATES = {"never": "never succeeded on this machine", "overdue": "overdue"}
ATTENTION_KINDS = frozenset({"sensor", "routine", "assistant", "channel"})
visible = _bf.visible
code = _bf.code


def plain(value: object) -> str:
    """Titles become link labels: one line, a Markdown link reduced to its label, without link delimiters."""
    return _bf.plain(value, fallback="untitled")


def page(brain: str, ref: str = "", offset: int = 0) -> dict[str, Any]:
    """One `bf read` page; an incomplete or unreadable page stops the review before any output."""
    name = ref or "home"
    args = ["read", *([ref] if ref else []), "--brain", brain, *(["--offset", str(offset)] if offset else [])]
    try:
        return _bf.bf_json(*args, timeout=TIMEOUT, max_bytes=REPLY_BYTES)
    except _bf.BfError as error:
        message = str(error)
        if "incomplete" in message:
            raise SystemExit(f"weekly-review: the {name} page is incomplete; run bf status") from None
        if "not valid JSON" in message:
            raise SystemExit(f"weekly-review: the {name} page is not JSON") from None
        raise SystemExit(f"weekly-review: could not read the {name} page") from None


def named(brain: str) -> str:
    """This brain's configured name, which `bf status --brain` lists alone; any failure stops the review."""
    try:
        reply = _bf.bf_json("status", "--brain", brain, timeout=TIMEOUT, allow_stale=True, max_bytes=REPLY_BYTES)
        listed = reply.get("brains", [])
        names = [
            entry["brain"]
            for entry in (listed if isinstance(listed, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("brain"), str)
        ]
    except _bf.BfError:
        names = []
    if len(names) != 1:
        raise SystemExit("weekly-review: could not name this brain; run bf status")
    return names[0]


def projects(brain: str) -> list[dict[str, Any]]:
    """The home page is a preview; enumerate projects before claiming none needs review."""
    result: list[dict[str, Any]] = []
    try:
        for reply in _bf.bf_pages("projects", brain=brain, timeout=TIMEOUT, max_pages=MAX_PROJECT_PAGES):
            items = reply.get("items", [])
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise SystemExit("weekly-review: the projects page is malformed; run bf status")
            result.extend(item for item in items if item.get("review"))
    except _bf.BfError as error:
        message = str(error)
        if "incomplete" in message:
            raise SystemExit("weekly-review: the projects page is incomplete; run bf status") from None
        raise SystemExit("weekly-review: the projects listing is incomplete; narrow the selected brain") from None
    return result


def local(value: object) -> str:
    """The local date of a returned time or date: replies state times with their offset, and a date stays its day."""
    try:
        return datetime.fromisoformat(str(value)).astimezone().date().isoformat()
    except ValueError:
        return ""


def when(item: dict[str, Any]) -> str:
    """An item's day or local time as the reply states it: a dated note has no time, a time keeps its offset."""
    if item.get("date"):
        return plain(item["date"])
    try:
        return datetime.fromisoformat(str(item.get("time", ""))).isoformat(sep=" ", timespec="minutes")
    except ValueError:
        return "undated"


def counted(number: object, noun: str) -> str:
    """A count with its noun, singular for one."""
    return f"{number} {noun}{'' if number == 1 else 's'}"


def began(item: dict[str, Any]) -> str:
    """The day an action's folder names, as bf validate requires `actions/YYYY-MM-DD_slug`; empty otherwise."""
    found = re.match(r"actions/(\d{4}-\d{2}-\d{2})_", str(item.get("ref", "")))
    return found[1] if found else ""


def mention(item: dict[str, Any]) -> str:
    """Name an item without linking it: a link from this dated action would count as newer evidence.

    Collected text (an invite's title, a mail subject) never enters the action: only its ref does. With several
    brains, the portable uri names the item's brain, as two brains can hold the same ref.
    """
    ref = item.get("uri") or item["ref"]
    if item.get("kind") == "record":
        return f"{code(ref)} (record)"
    return f"{plain(item.get('title', item['ref']))} ({code(ref)})"


def attention(home: dict[str, Any]) -> list[str]:
    """One line per program the home page says failed, is late, fell silent or keeps missing parts.

    Program names come from bf.yaml; code spans keep them inert.
    """
    lines = []
    for entry in home.get("attention", []):
        if not isinstance(entry, dict):
            continue
        kind, name = entry.get("kind"), entry.get("name")
        if kind not in ATTENTION_KINDS or not isinstance(name, str) or not name:
            continue
        where = f" in brain {code(entry['brain'])}" if entry.get("brain") else ""
        state = [STATES[entry["freshness"]]] if entry.get("freshness") in STATES else []
        state += ["last attempt failed"] if entry.get("failed") else []
        state += ["no new or changed record within its `silent_after`"] if entry.get("silent") else []
        state += ["recent runs missed parts; its log names them"] if entry.get("degraded") else []
        lines.append(f"- {kind.capitalize()} {code(name)}{where}: {'; '.join(state) or 'needs attention'}.")
    return lines


def _body(day: str, home: dict[str, Any], week: dict[str, Any]) -> Iterator[str]:
    late = attention(home)
    # Only sensors collect the evidence that the period and the coming week count.
    gap = any(isinstance(entry, dict) and entry.get("kind") == "sensor" for entry in home.get("attention", []))
    qualifier = ", and a sensor needing attention may have missed some" if gap else ""
    review = sorted(
        (project for project in home.get("projects", []) if project.get("review")),
        key=lambda project: ("due" not in project.get("review_reasons", []), project.get("review_due", "")),
    )
    yield f"# Weekly review {day}"
    yield ""
    # One line per paragraph keeps the action stable under Markdown formatters.
    yield (
        "Generated by the weekly-review routine from `bf read`, `bf read projects`, `bf read tasks` and `bf read 7d`. "
        "Update the owning notes and check off completed tasks there. "
        "Set `status: stable` when this review is ready to rely on. This action changes nothing by itself."
    )
    if late:
        yield ""
        yield "## Programs needing attention"
        yield ""
        yield from late
        yield ""
        yield "Check `bf status` and each program's log; a sensor that did not collect leaves its evidence out below."
    yield ""
    yield "## Projects to review"
    yield ""
    for project in review:
        details = [str(project.get("status", "")), f"edited {local(project.get('modified')) or 'unknown'}"]
        if project.get("review_due"):
            details.append(f"review due {local(project['review_due'])}")
        if project.get("new_links"):
            details.append(counted(project["new_links"], "newer linked item"))
        line = f"- {mention(project)} ({', '.join(details)})"
        yield line + (f". Next: {plain(project['next'])}" if project.get("next") else "")
    if not review:
        yield "No project note is due for review."
    yield ""
    yield "## Open tasks"
    yield ""
    tasks = home["tasks"]
    counts = tasks["summary"]
    yield (
        f"{counted(counts['open'], 'open task')}; {counts['done']} completed across "
        f"{counted(counts['notes'], 'note')} with checkboxes."
    )
    preview = tasks.get("items", [])[:TASK_PREVIEW]
    if preview:
        yield ""
    for task in preview:
        # Plain bullets and inert refs avoid creating duplicate tasks or new graph evidence.
        yield f"- {plain(task['text'])} ({code(task.get('uri') or task['ref'])}, line {task['line']})."
    if tasks["total"] > len(preview):
        yield ""
        yield f"Showing {len(preview)} of {tasks['total']} open tasks; use `bf read tasks` and follow `next_offset`."
    yield ""
    yield "## Last seven days"
    yield ""
    sources = ", ".join(f"{plain(s['source'])} {s['records']}" for s in week.get("sources", []))
    period = f"- {counted(week.get('total', 0), 'item')} in the period"
    if sources:
        period += f"; records by source: {sources}"
    elif not week.get("total"):
        period += qualifier
    yield period + "."
    # The days around this one that `bf read SINCE..DAY` and `bf read DAY..AHEAD` list in full.
    today = date.fromisoformat(day)
    since, ahead = (today - timedelta(days=7)).isoformat(), (today + timedelta(days=7)).isoformat()
    if home.get("changed"):
        yield "- Notes changed: " + ", ".join(mention(item) for item in home["changed"])
    if len(home.get("changed", [])) >= HOME_PREVIEW:
        yield (
            f"- Showing the newest {HOME_PREVIEW} changed notes; use `bf read {since}..{day}` and follow `next_offset`."
        )
    # The home page lists the newest open (`status: draft`) action folders: keep those begun within the period.
    actions = home.get("actions", [])
    recent = [item for item in actions if since <= began(item) <= day]
    if recent:
        yield "- Recent actions: " + ", ".join(mention(item) for item in recent)
    # While every previewed action began within the period or later, a planned one included, earlier actions of the
    # period may lie beyond the preview.
    if len(actions) >= ACTION_PREVIEW and all(began(item) >= since for item in actions):
        yield (
            f"- Showing the period's actions among the newest {ACTION_PREVIEW}; "
            "use `bf read actions` and follow `next_offset`."
        )
    yield ""
    yield "## Coming week"
    yield ""
    for item in home.get("upcoming", []):
        yield f"- {when(item)}: {mention(item)}"
    if not home.get("upcoming"):
        yield f"No upcoming item was collected{qualifier}."
    elif len(home["upcoming"]) >= HOME_PREVIEW:
        yield ""
        yield f"Showing the first {HOME_PREVIEW} upcoming items; use `bf read {day}..{ahead}` and follow `next_offset`."
    yield ""
    yield "## Resume"
    yield ""
    resume = (
        "Review the listed projects and open tasks in their owning notes; record decisions there with evidence refs."
    )
    yield f"Repair the programs needing attention first. {resume}" if late else resume


def _frontmatter(day: str) -> dict[str, str]:
    return {
        "status": "draft",
        "updated": day,
        "summary": "Weekly review of projects, actions and evidence from the last seven days.",
    }


def render(day: str, home: dict[str, Any], week: dict[str, Any]) -> Iterator[str]:
    """The review action: an empty section says so when a late, failing or silent sensor may explain it."""
    yield from _bf.format_action(_frontmatter(day), _body(day, home, week)).splitlines()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", argv[2]):
        sys.stderr.write("usage: weekly-review.py BRAIN END\n")
        return 2
    brain = argv[1]
    try:
        # The action folder is named after the local date of the run; keep `updated` on the same day.
        day = datetime.fromisoformat(argv[2]).astimezone().date().isoformat()
    except ValueError:
        sys.stderr.write("usage: weekly-review.py BRAIN END\n")
        return 2
    home, week = page(brain), page(brain, "7d")
    # BF records a routine's run only after it exits: this run's own entry would call it late or failed. The guides
    # name the routine after its file. With brains declared under `brains:`, entries name their brain, and another
    # brain's routine of this name stays listed.
    entries = home.get("attention", [])
    own = named(brain) if any(isinstance(entry, dict) and "brain" in entry for entry in entries) else ""
    self_name = Path(argv[0]).stem
    home["attention"] = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and entry.get("kind") == "routine"
            and entry.get("name") == self_name
            and entry.get("brain", own) == own
        )
    ]
    home["projects"] = projects(brain)
    home["tasks"] = page(brain, "tasks")
    if (
        not any(p.get("review") for p in home.get("projects", []))
        and not week.get("total")
        and not home["tasks"]["total"]
        and not attention(home)
    ):
        return 0  # Nothing to review: no output, so no action is written.
    _bf.emit_action(_frontmatter(day), _body(day, home, week))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
