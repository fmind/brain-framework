#!/usr/bin/env python3
"""Weekly review: render the brain's home page and last seven days as a review action; no model involved."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPLY_BYTES = 4 << 20
MAX_PAGES = 100
MAX_ACTION_BYTES = 4 << 20
TIMEOUT = 120
TASK_PREVIEW = 10
# The home page previews at most 20 changed notes and 20 upcoming items, without a total.
HOME_PREVIEW = 20
# It previews the 10 newest open (`status: draft`) action folders, without a total.
ACTION_PREVIEW = 10
# How the home page's attention entries describe a scheduled job's freshness on this machine.
STATES = {"never": "never succeeded on this machine", "overdue": "overdue"}
ATTENTION_KINDS = frozenset({"job"})


def visible(value: str) -> str:
    """Escape control and invisible Unicode format characters so hidden controls stay explicit."""
    parts: list[str] = []
    for ch in value:
        cat = unicodedata.category(ch)
        if cat in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            code_pt = ord(ch)
            parts.append(f"\\u{code_pt:04x}" if code_pt <= 0xFFFF else f"\\U{code_pt:08x}")
        else:
            parts.append(ch)
    return "".join(parts)


def plain(value: object, *, fallback: str = "") -> str:
    """One plain single-line label: unwrap Markdown link syntax `[label](url)` and escape hidden controls."""
    raw = re.sub(r"[ \t\r\n]+", " ", str(value or "")).strip()
    if not raw:
        return fallback
    unwrapped = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", raw)
    cleaned = re.sub(r"[ \t\r\n]+", " ", re.sub(r"[\[\]`<>]", "", unwrapped)).strip()
    return visible(cleaned) if cleaned else fallback


def code(value: object) -> str:
    """Render an untrusted identifier inside an inert Markdown inline code span."""
    text = visible(str(value or ""))
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    if text.startswith("`") or text.endswith("`") or (longest > 0 and (text.startswith(" ") or text.endswith(" "))):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def _bf_cmd() -> str:
    bf = shutil.which("bf")
    if bf is None:
        raise SystemExit("weekly-review: bf is not on PATH")
    return bf


def bf_json(*args: str, timeout: int = TIMEOUT, allow_stale: bool = False, max_bytes: int = REPLY_BYTES) -> Any:
    """Run one read-only `bf` command and decode its bounded JSON reply, failing closed on problems or stale data."""
    bf = _bf_cmd()
    try:
        res = subprocess.run(  # noqa: S603 - explicit executable and controlled CLI args
            [bf, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"weekly-review: bf {' '.join(args[:2])} failed") from error
    if res.returncode != 0 or len(res.stdout) > max_bytes:
        raise SystemExit(f"weekly-review: bf {' '.join(args[:2])} failed")
    try:
        data = json.loads(res.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"weekly-review: bf {' '.join(args[:2])} returned not valid JSON") from error
    if isinstance(data, dict) and (data.get("problems") or (not allow_stale and data.get("stale"))):
        raise SystemExit(
            f"weekly-review: the {args[1] if len(args) > 1 and not args[1].startswith('-') else 'home'} page is incomplete; run bf status"
        )
    return data


def bf_pages(
    ref: str, *, brain: str = "", timeout: int = TIMEOUT, max_pages: int = MAX_PAGES
) -> Iterator[dict[str, Any]]:
    """Yield every page of a paginated `bf read` listing up to `max_pages`."""
    offset: int | None = None
    for _ in range(max_pages):
        args = ["read", ref, *(["--brain", brain] if brain else []), *(["--offset", str(offset)] if offset else [])]
        reply = bf_json(*args, timeout=timeout)
        if not isinstance(reply, dict):
            raise SystemExit(f"weekly-review: the {ref} page is malformed; run bf status")
        yield reply
        nxt = reply.get("next_offset")
        if nxt is None:
            return
        if not isinstance(nxt, int) or isinstance(nxt, bool) or nxt <= (offset or 0) or nxt >= 2**53:
            raise SystemExit(f"weekly-review: the {ref} page is malformed; run bf status")
        offset = nxt
    raise SystemExit(f"weekly-review: the {ref} listing is incomplete; narrow the selected brain")


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-JSON float in action frontmatter")
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError(f"unsupported scalar in action frontmatter: {type(value).__name__}")


def _yaml_value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = []
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("frontmatter mapping keys must be strings")
            pairs.append(f"{json.dumps(k, ensure_ascii=False)}: {_yaml_value(v)}")
        return "{" + ", ".join(pairs) + "}"
    return _yaml_scalar(value)


def format_action(frontmatter: Mapping[str, object], body: Iterable[str]) -> str:
    """Render an OKF `action` Markdown note with JSON-safe YAML frontmatter."""
    attrs: dict[str, object] = {"type": "action", **frontmatter}
    lines = ["---"]
    for key, value in attrs.items():
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.append("---")
    lines.extend(body)
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > MAX_ACTION_BYTES:
        raise ValueError("rendered action exceeds maximum byte budget")
    return rendered


def page(brain: str, ref: str = "", offset: int = 0) -> dict[str, Any]:
    """One `bf read` page; an incomplete or unreadable page stops the review before any output."""
    name = ref or "home"
    args = ["read", *([ref] if ref else []), "--brain", brain, *(["--offset", str(offset)] if offset else [])]
    data = bf_json(*args, timeout=TIMEOUT, max_bytes=REPLY_BYTES)
    if not isinstance(data, dict):
        raise SystemExit(f"weekly-review: the {name} page is malformed; run bf status")
    return data


def named(brain: str) -> str:
    """This brain's configured name, which `bf status --brain` returns; any failure stops the review."""
    try:
        reply = bf_json("status", "--brain", brain, timeout=TIMEOUT, allow_stale=True, max_bytes=REPLY_BYTES)
        name = reply.get("brain") if isinstance(reply, dict) else None
    except SystemExit:
        name = None
    if not isinstance(name, str) or not name:
        raise SystemExit("weekly-review: could not name this brain; run bf status")
    return name


def projects(brain: str) -> list[dict[str, Any]]:
    """The home page is a preview; enumerate projects before claiming none needs review."""
    result: list[dict[str, Any]] = []
    for reply in bf_pages("projects", brain=brain, timeout=TIMEOUT, max_pages=MAX_PAGES):
        items = reply.get("items", [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise SystemExit("weekly-review: the projects page is malformed; run bf status")
        result.extend(item for item in items if item.get("review"))
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

    Collected text never enters the action: only record refs do. With several brains, the portable uri names the
    item's brain, as two brains can hold the same ref.
    """
    ref = item.get("uri") or item["ref"]
    if item.get("kind") == "record":
        return f"{code(ref)} (record)"
    return f"{plain(item.get('title', item['ref']), fallback='untitled')} ({code(ref)})"


def attention(home: dict[str, Any]) -> list[str]:
    """One line per job the home page says failed, is late, fell silent or keeps missing parts.

    Job names come from bf.yaml; code spans keep them inert.
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
    qualifier = ", and a job needing attention may have missed some" if late else ""
    review = sorted(
        (project for project in home.get("projects", []) if project.get("review")),
        key=lambda project: ("due" not in project.get("review_reasons", []), project.get("review_due", "")),
    )
    yield f"# Weekly review {day}"
    yield ""
    # One line per paragraph keeps the action stable under Markdown formatters.
    yield (
        "Generated by the weekly-review job from `bf read`, `bf read projects`, `bf read tasks` and `bf read 7d`. "
        "Update the owning notes and check off completed tasks there. "
        "Set `status: stable` when this review is ready to rely on. This action changes nothing by itself."
    )
    if late:
        yield ""
        yield "## Jobs needing attention"
        yield ""
        yield from late
        yield ""
        yield "Check `bf status` and each job's log; a collection job that did not collect leaves its evidence out below."
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
        yield f"- Showing the newest {HOME_PREVIEW} changed notes; use `bf read {since}..{day}` and follow `next_offset`."
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
    yield f"Repair the jobs needing attention first. {resume}" if late else resume


def _frontmatter(day: str) -> dict[str, str]:
    return {
        "status": "draft",
        "updated": day,
        "summary": "Weekly review of projects, actions and evidence from the last seven days.",
    }


def render(day: str, home: dict[str, Any], week: dict[str, Any]) -> Iterator[str]:
    """The review action: an empty section says so when a late, failing or silent job may explain it."""
    yield from format_action(_frontmatter(day), _body(day, home, week)).splitlines()


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
    # BF records a job's run only after it exits: filter this running job's own attention entry out of its report.
    entries = home.get("attention", [])
    own = named(brain) if any(isinstance(entry, dict) and "brain" in entry for entry in entries) else ""
    self_name = Path(argv[0]).stem
    home["attention"] = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and entry.get("kind") == "job"
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
    sys.stdout.write(format_action(_frontmatter(day), _body(day, home, week)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
