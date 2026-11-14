#!/usr/bin/env python3
"""Check Brain Framework queue incidents, failing or degraded jobs, and notes flagged for review.

Reads `bf status` and `bf read review` (all pages) offline, failing closed on status problems,
cache staleness, malformed replies, or bad offsets. When incidents, queue errors, attention/failed
jobs, or review-flagged notes exist, prints one draft OKF ACTION.md on stdout; otherwise prints
nothing so BF skips creating an action. Safe to invoke from `bf run` / `bf update` with `emit: action`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

TIMEOUT = 30.0
MAX_BYTES = 16 * 1024 * 1024
MAX_PAGES = 100
_STATES = {"overdue": "overdue", "never": "due, never collected"}


def visible(value: str) -> str:
    """Replace control, line-break, and invisible formatting characters so untrusted strings stay single-line."""
    return "".join(" " if ord(ch) < 32 or ord(ch) == 127 or 0x200B <= ord(ch) <= 0x206F else ch for ch in value)


def plain(value: object, *, fallback: str = "") -> str:
    text = " ".join(visible(str(value or "")).split())
    return text or fallback


def code(value: object) -> str:
    """Wrap an untrusted identifier in backticks safely without allowing nested fence breaks."""
    text = visible(str(value or ""))
    run = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0) + 1
    fence = "`" * run
    if text.startswith("`") or text.endswith("`"):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def bf_json(*args: str) -> dict[str, Any]:
    """Run one read-only `bf` command without a shell and decode its compact JSON object."""
    bf = shutil.which("bf")
    if bf is None:
        raise SystemExit("evidence-health: bf is not on PATH")
    try:
        proc = subprocess.run(  # noqa: S603 - resolved executable path and literal argument vector
            [bf, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except OSError as error:
        raise SystemExit(f"evidence-health: failed to invoke bf {args[0]}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"evidence-health: bf {args[0]} timed out after {TIMEOUT:g}s") from error
    if proc.returncode != 0:
        detail = plain(proc.stderr, fallback=f"exit {proc.returncode}")
        raise SystemExit(f"evidence-health: bf {args[0]} failed: {detail}")
    if proc.stderr.strip():
        raise SystemExit(f"evidence-health: bf {args[0]} emitted warnings on stderr")
    if len(proc.stdout.encode("utf-8")) > MAX_BYTES:
        raise SystemExit(f"evidence-health: bf {args[0]} exceeded {MAX_BYTES} bytes")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(f"evidence-health: invalid JSON from bf {args[0]}: {error}") from error
    if not isinstance(data, dict):
        raise SystemExit(f"evidence-health: expected JSON object from bf {args[0]}")
    if data.get("problems"):
        raise SystemExit("evidence-health: brain reports retrieval or validation problems")
    if data.get("stale"):
        raise SystemExit("evidence-health: search cache is stale; retry after refresh completes")
    return data


def review_pages(brain: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read every page of `bf read review` safely with strict offset validation."""
    items: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    seen_offsets: set[int] = set()
    offset = 0
    for _ in range(MAX_PAGES):
        args = ["read", "review", "--brain", brain]
        if offset:
            args.extend(["--offset", str(offset)])
        reply = bf_json(*args)
        if reply.get("page") != "review":
            raise SystemExit("evidence-health: unexpected reply from bf read review")
        page_items = reply.get("items", [])
        if not isinstance(page_items, list) or not all(isinstance(entry, dict) for entry in page_items):
            raise SystemExit("evidence-health: malformed review items")
        items.extend(page_items)
        if offset == 0:
            raw_attention = reply.get("attention", [])
            if not isinstance(raw_attention, list) or not all(isinstance(entry, dict) for entry in raw_attention):
                raise SystemExit("evidence-health: malformed attention list")
            attention.extend(raw_attention)
        next_offset = reply.get("next_offset")
        if next_offset is None:
            return items, attention
        if not isinstance(next_offset, int) or next_offset <= offset or next_offset in seen_offsets:
            raise SystemExit("evidence-health: non-advancing next_offset in review reply")
        seen_offsets.add(offset)
        offset = next_offset
    raise SystemExit("evidence-health: review listing exceeded maximum page count")


def _health_notes(entry: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    freshness = entry.get("freshness")
    if isinstance(freshness, str) and freshness in _STATES:
        notes.append(_STATES[freshness])
    if entry.get("failed"):
        failures = entry.get("failures")
        notes.append(f"failed ({failures} consecutive)" if isinstance(failures, int) and failures > 1 else "failed")
    if entry.get("silent"):
        notes.append("silent past silent_after")
    degraded = entry.get("degraded")
    if isinstance(degraded, dict):
        notes.append(
            f"degraded ({degraded.get('missed', '?')}/{degraded.get('of', '?')} parts over {degraded.get('runs', '?')} runs)"
        )
    elif degraded:
        notes.append("degraded in consecutive runs")
    return notes


def _format_action(day: str, body: Iterator[str]) -> str:
    lines = [
        "---",
        "type: action",
        "status: draft",
        f"updated: {day}",
        "summary: Evidence and job health findings requiring attention or review.",
        "---",
        "",
        *body,
    ]
    return "\n".join(lines).rstrip() + "\n"


def _body(
    day: str,
    queue_issues: list[str],
    job_issues: list[str],
    source_issues: list[str],
    review_items: list[dict[str, Any]],
) -> Iterator[str]:
    yield f"# Evidence health {day}"
    yield ""
    yield (
        "Generated from `bf status` and `bf read review`. Inspect affected logs, fix failing jobs or queue "
        "obligations, and update the owning notes directly (refs stay plain backticks so this draft creates no links). "
        "Set `status: stable` once handled."
    )
    if queue_issues:
        yield ""
        yield "## Incidents and queue"
        yield ""
        yield from queue_issues
    if job_issues or source_issues:
        yield ""
        yield "## Jobs and sources needing attention"
        yield ""
        yield from job_issues
        yield from source_issues
    if review_items:
        yield ""
        yield "## Notes needing review"
        yield ""
        for item in review_items:
            ref = item.get("uri") or item.get("ref") or "unknown"
            title = plain(item.get("title"), fallback="untitled")
            reasons = [plain(r) for r in item.get("review_reasons", []) if r]
            due_date = plain(item.get("review_due"))
            if due_date:
                reasons = [f"due {due_date}" if r == "due" else r for r in reasons]
                if f"due {due_date}" not in reasons:
                    reasons.append(f"due {due_date}")
            details = reasons or ["flagged for review"]
            if item.get("new_links"):
                details.append(f"{item['new_links']} newer linked")
            yield f"- {title} ({code(ref)}): {', '.join(details)}."
    yield ""
    yield "## Resume"
    yield ""
    yield "Run `bf status` and `bf queue` in the brain, inspect any reported job logs, and clear review flags in owning notes."


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", argv[2]):
        sys.stderr.write("usage: evidence-health.py BRAIN END\n")
        return 2
    brain = argv[1]
    try:
        day = datetime.fromisoformat(argv[2]).astimezone().date().isoformat()
    except ValueError:
        sys.stderr.write("usage: evidence-health.py BRAIN END\n")
        return 2

    status = bf_json("status", "--brain", brain)
    if status.get("cache") in ("stale", "missing") or status.get("pending_transaction"):
        raise SystemExit(f"evidence-health: cache state is {status.get('cache', 'incomplete')}; retry after refresh")

    self_name = Path(argv[0]).stem
    queue = status.get("queue", {})
    if not isinstance(queue, dict):
        raise SystemExit("evidence-health: malformed queue summary in status reply")

    queue_issues: list[str] = []
    incidents = int(queue.get("incidents", 0) or 0)
    failed_deliveries = int(queue.get("failed", 0) or 0)
    uncertain_deliveries = int(queue.get("uncertain", 0) or 0)
    if incidents:
        queue_issues.append(f"- Open failure incidents: {incidents} (check `bf status` and offending job logs).")
    if failed_deliveries:
        queue_issues.append(f"- Permanently failed queue deliveries: {failed_deliveries} (inspect `bf queue`).")
    if uncertain_deliveries:
        queue_issues.append(
            f"- Uncertain queue deliveries awaiting manual verification/retry: {uncertain_deliveries} (`bf queue --retry`)."
        )

    jobs = status.get("jobs", {})
    sources = status.get("sources", {})
    if not isinstance(jobs, dict) or not isinstance(sources, dict):
        raise SystemExit("evidence-health: malformed jobs/sources map in status reply")

    job_issues: list[str] = []
    seen_jobs: set[str] = set()
    for name, info in sorted(jobs.items()):
        if not isinstance(info, dict) or (name == self_name and not info.get("failed")):
            continue
        notes = _health_notes(info)
        if not notes:
            continue
        seen_jobs.add(name)
        suffix = f" (log: {code(info['log'])})" if info.get("log") else ""
        if info.get("error"):
            suffix += f": {plain(info['error'])}"
        job_issues.append(f"- Job {code(name)}: {'; '.join(notes)}{suffix}.")

    review_items, attention = review_pages(brain)
    for entry in attention:
        name = str(entry.get("name") or "")
        if not name or name in seen_jobs or (name == self_name and not entry.get("failed")):
            continue
        notes = _health_notes(entry)
        if notes:
            job_issues.append(f"- Job {code(name)}: {'; '.join(notes)}.")

    source_issues: list[str] = []
    for source, info in sorted(sources.items()):
        if not isinstance(info, dict):
            continue
        notes = _health_notes(info)
        if notes and source not in seen_jobs:
            source_issues.append(f"- Source {code(source)}: {'; '.join(notes)}.")

    if not queue_issues and not job_issues and not source_issues and not review_items:
        return 0

    sys.stdout.write(_format_action(day, _body(day, queue_issues, job_issues, source_issues, review_items)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
