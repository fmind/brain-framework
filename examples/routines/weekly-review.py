#!/usr/bin/env python3
"""Weekly review: render the brain's home page and last seven days as a review action; no model involved."""

import json
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

REPLY_BYTES = 4 << 20
TIMEOUT = 120
MAX_PROJECT_PAGES = 100
TASK_PREVIEW = 10
# The home page previews at most 20 changed notes and 20 upcoming items, without a total.
HOME_PREVIEW = 20
# It previews the 10 newest action folders, of any date, without a total.
ACTION_PREVIEW = 10
# How the home page's attention entries describe a scheduled program's freshness on this machine.
STATES = {"never": "never succeeded on this machine", "overdue": "overdue"}


def page(brain: str, ref: str = "", offset: int = 0) -> dict:
    """One `bf read` page; an incomplete or unreadable page stops the review before any output."""
    name = ref or "home"
    try:
        result = subprocess.run(  # noqa: S603 - literal argv; bf is the brain's own offline reader
            ["bf", "read", *([ref] if ref else []), "--brain", brain, *(["--offset", str(offset)] if offset else [])],  # noqa: S607 - bf from the owner's PATH
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit(f"weekly-review: could not read the {name} page") from None
    if result.returncode or len(result.stdout) > REPLY_BYTES:
        raise SystemExit(f"weekly-review: could not read the {name} page")
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        raise SystemExit(f"weekly-review: the {name} page is not JSON") from None
    if not isinstance(reply, dict) or reply.get("problems") or reply.get("stale"):
        # A partial page could hide a project due for review: report nothing rather than too little.
        raise SystemExit(f"weekly-review: the {name} page is incomplete; run bf status")
    return reply


def named(brain: str) -> str:
    """This brain's configured name, which `bf status --brain` lists alone; any failure stops the review."""
    try:
        result = subprocess.run(  # noqa: S603 - literal argv; bf is the brain's own offline reader
            ["bf", "status", "--brain", brain],  # noqa: S607 - bf from the owner's PATH
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
        listed = (
            json.loads(result.stdout)["brains"] if not result.returncode and len(result.stdout) <= REPLY_BYTES else []
        )
        names = [entry["brain"] for entry in listed if isinstance(entry, dict) and isinstance(entry.get("brain"), str)]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        names = []
    if len(names) != 1:
        raise SystemExit("weekly-review: could not name this brain; run bf status")
    return names[0]


def projects(brain: str) -> list[dict]:
    """The home page is a preview; enumerate projects before claiming none needs review."""
    result = []
    offset = 0
    for _ in range(MAX_PROJECT_PAGES):
        reply = page(brain, "projects", offset)
        result.extend(item for item in reply.get("items", []) if item.get("review"))
        if "next_offset" not in reply:
            return result
        following = reply["next_offset"]
        if type(following) is not int or following <= offset:
            break
        offset = following
    raise SystemExit("weekly-review: the projects listing is incomplete; narrow the selected brain")


def visible(text: str) -> str:
    """Escape controls, line and paragraph separators and invisible format characters, which bf replies escape and
    JSON decoding restored."""
    return "".join(
        char.encode("unicode_escape").decode() if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in text
    )


def plain(value: object) -> str:
    """Titles become link labels: one line, a Markdown link reduced to its label, without link delimiters."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", str(value))
    return visible(re.sub(r"\s+", " ", re.sub(r"[\[\]()<>`]", "", text)).strip()) or "untitled"


def local(value: object) -> str:
    """The local date of a returned time or date: replies state times with their offset, and a date stays its day."""
    try:
        return datetime.fromisoformat(str(value)).astimezone().date().isoformat()
    except ValueError:
        return ""


def when(item: dict) -> str:
    """An item's day or local time as the reply states it: a dated note has no time, a time keeps its offset."""
    if item.get("date"):
        return plain(item["date"])
    try:
        return datetime.fromisoformat(str(item.get("time", ""))).isoformat(sep=" ", timespec="minutes")
    except ValueError:
        return "undated"


def code(value: object) -> str:
    """A code span no backtick inside a record id can close, so a ref never becomes Markdown or a link."""
    text = visible(str(value))
    fence = "`" * (max(map(len, re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def counted(number: object, noun: str) -> str:
    """A count with its noun, singular for one."""
    return f"{number} {noun}{'' if number == 1 else 's'}"


def began(item: dict) -> str:
    """The day an action's folder names, as bf validate requires `actions/YYYY-MM-DD_slug`; empty otherwise."""
    found = re.match(r"actions/(\d{4}-\d{2}-\d{2})_", str(item.get("ref", "")))
    return found[1] if found else ""


def mention(item: dict) -> str:
    """Name an item without linking it: a link from this dated action would count as newer evidence.

    Collected text (an invite's title, a mail subject) never enters the action: only its ref does. With several
    brains, the portable uri names the item's brain, as two brains can hold the same ref.
    """
    ref = item.get("uri") or item["ref"]
    if item.get("kind") == "record":
        return f"{code(ref)} (record)"
    return f"{plain(item.get('title', item['ref']))} ({code(ref)})"


def attention(home: dict) -> list[str]:
    """One line per scheduled program the home page says failed, is late on this machine or, for a source, fell silent.

    Program names come from bf.yaml; code spans keep them inert.
    """
    lines = []
    for entry in home.get("attention", []):
        kind = next((kind for kind in ("sensor", "routine", "assistant") if entry.get(kind)), "")
        if not kind:
            continue
        where = f" in brain {code(entry['brain'])}" if entry.get("brain") else ""
        state = [STATES[entry["freshness"]]] if entry.get("freshness") in STATES else []
        state += ["last attempt failed"] if entry.get("failed") else []
        state += ["no new or changed record within its `silent_after`"] if entry.get("silent") else []
        lines.append(f"- {kind.capitalize()} {code(entry[kind])}{where}: {'; '.join(state) or 'needs attention'}.")
    return lines


def render(day: str, home: dict, week: dict) -> Iterator[str]:
    """The review action: an empty section says so when a late, failing or silent sensor may explain it."""
    late = attention(home)
    # Only sensors collect the evidence that the period and the coming week count.
    gap = any(entry.get("sensor") for entry in home.get("attention", []))
    qualifier = ", and a sensor needing attention may have missed some" if gap else ""
    review = sorted(
        (project for project in home.get("projects", []) if project.get("review")),
        key=lambda project: ("due" not in project.get("review_reasons", []), project.get("review_due", "")),
    )
    yield "---"
    yield "type: action"
    yield "status: draft"
    yield f"updated: {day}"
    yield "summary: Weekly review of projects, actions and evidence from the last seven days."
    yield "---"
    yield ""
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
    yield period
    # The days around this one that `bf read SINCE..DAY` and `bf read DAY..AHEAD` list in full.
    today = date.fromisoformat(day)
    since, ahead = (today - timedelta(days=7)).isoformat(), (today + timedelta(days=7)).isoformat()
    if home.get("changed"):
        yield "- Notes changed: " + ", ".join(mention(item) for item in home["changed"])
    if len(home.get("changed", [])) >= HOME_PREVIEW:
        yield (
            f"- Showing the newest {HOME_PREVIEW} changed notes; use `bf read {since}..{day}` and follow `next_offset`."
        )
    # The home page lists the newest action folders, past or planned: keep those begun within the period.
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
    own = named(brain) if any("brain" in entry for entry in entries) else ""
    home["attention"] = [
        entry for entry in entries if entry.get("routine") != Path(argv[0]).stem or entry.get("brain", own) != own
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
    sys.stdout.write("\n".join(render(day, home, week)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
