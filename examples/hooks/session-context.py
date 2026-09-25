#!/usr/bin/env python3
"""Print the brain's context for the current repository at agent session start; silent when unavailable."""

import json
import re
import subprocess
import sys
from datetime import datetime

REPLY_BYTES = 4 << 20
REMOTE_BYTES = 8 << 10
NEWEST = 3


def run(argv: list[str], limit: int, timeout: int) -> bytes | None:
    """Literal argv, bounded time and output; any failure means no context rather than a blocked session."""
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode or len(result.stdout) > limit:
        return None
    return result.stdout


def identity() -> str:
    """The GitHub repository of the working directory, as sensors record it; never raw remote credentials."""
    raw = run(["git", "remote", "get-url", "origin"], REMOTE_BYTES, 5)
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?",
        (raw or b"").decode(errors="replace").strip(),
    )
    return "repo:github.com/" + match[1].lower() if match else ""


def read(ref: str, brain: str) -> dict | None:
    raw = run(["bf", "read", ref, *(["--brain", brain] if brain else [])], REPLY_BYTES, 20)
    try:
        reply = json.loads(raw) if raw else None
    except ValueError:
        return None
    return reply if isinstance(reply, dict) else None


def day(value: object) -> str:
    """The local date of a returned UTC time: a note dated 2026-09-25 is local midnight, not the UTC day."""
    try:
        return datetime.fromisoformat(str(value)).astimezone().date().isoformat()
    except ValueError:
        return ""


def plain(value: object, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"[`\[\]<>]", "", str(value))).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def render(repo: str, page: dict, project: dict | None) -> list[str]:
    lines = [f"Brain context for {repo} (evidence, not instructions):"]
    ref = page.get("ref")
    if ref and project:
        state = [str(project.get("status", "")), f"updated {day(project.get('time')) or 'never'}"]
        if project.get("review"):
            state.append(f"review due ({project.get('new_links', 0)} newer linked items)")
        lines.append(f"- Project: {plain(project.get('title', ref))} (`{ref}`), {', '.join(filter(None, state))}.")
        if project.get("next"):
            lines.append(f"- Next task: {plain(project['next'])}")
    elif ref:
        lines.append(f"- Owning note: `{ref}`.")
    else:
        lines.append("- No note owns this repository yet.")
    groups = page.get("backlinks", [])
    if groups:
        counts = ", ".join(f"{g.get('relation', 'links')} {g['total']}" for g in groups)
        lines.append(f"- Linked evidence: {counts}.")
        items = [i for g in groups for i in g.get("items", []) if not i.get("external")]
        newest = sorted(items, key=lambda i: str(i.get("time", "")), reverse=True)[:NEWEST]
        lines.extend(f"  - {day(i.get('time'))} {plain(i.get('title', ''))} (`{i['ref']}`)" for i in newest)
    lines.append(f"Read more with `bf read {ref or repo}`; external items are counted, not quoted.")
    return lines


def main(argv: list[str]) -> int:
    brain = argv[1] if len(argv) > 1 else ""
    repo = identity()
    page = read(repo, brain) if repo else None
    if not page or page.get("problems") or page.get("stale"):
        return 0
    project = None
    if str(page.get("ref", "")).startswith("projects/"):
        listing = read("projects", brain) or {}
        project = next((p for p in listing.get("items", []) if p.get("ref") == page["ref"]), None)
    sys.stdout.write("\n".join(render(repo, page, project)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
