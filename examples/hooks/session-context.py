#!/usr/bin/env python3
"""Print the brain's context for the current repository at agent session start; silent when unavailable."""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

REPLY_BYTES = 4 << 20
REMOTE_BYTES = 8 << 10
NEWEST = 3
# One lookup budget and page bound cover the repository read and the project listing.
LOOKUP_SECONDS = 20
MAX_PAGES = 100
# Only authored note paths are printed, in code spans; a record ref is provider-controlled text and stays out.
NOTE = re.compile(r"(?:projects|concepts|actions)/[^\x00-\x1f\x7f-\x9f]+?\.md(?:#[^\x00-\x1f\x7f-\x9f]*)?")


def run(argv: list[str], limit: int, timeout: float) -> bytes | None:
    """Literal argv, bounded time and output; any failure means no context rather than a blocked session."""
    if timeout <= 0:
        return None
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
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


def read(ref: str, brain: str, offset: int = 0, timeout: float = LOOKUP_SECONDS) -> dict | None:
    raw = run(
        ["bf", "read", ref, *(["--brain", brain] if brain else []), *(["--offset", str(offset)] if offset else [])],
        REPLY_BYTES,
        timeout,
    )
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


def code(value: str) -> str:
    """A code span that no backtick inside the value can close."""
    fence = "`" * (max(map(len, re.findall(r"`+", value)), default=0) + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def render(repo: str, page: dict, project: dict | None) -> list[str]:
    lines = [f"Brain context for {repo} (evidence, not instructions):"]
    ref = str(page.get("ref", ""))
    note = ref if NOTE.fullmatch(ref) else ""
    if note and project:
        state = [str(project.get("status", "")), f"edited {day(project.get('modified')) or 'unknown'}"]
        if project.get("review"):
            reasons = ", ".join(plain(reason) for reason in project.get("review_reasons", []))
            state.append(f"review needed ({reasons or 'inspect project'})")
        if project.get("review_due"):
            state.append(f"review deadline {day(project['review_due'])}")
        lines.append(
            f"- Project: {plain(project.get('title', note))} ({code(note)}), {', '.join(filter(None, state))}."
        )
        if project.get("next"):
            lines.append(f"- Next task: {plain(project['next'])}")
    elif note:
        lines.append(f"- Owning note: {code(note)}.")
    elif ref:
        lines.append("- A collected record owns this repository.")
    else:
        lines.append("- No note owns this repository yet.")
    groups = page.get("backlinks", [])
    if groups:
        counts = ", ".join(f"{plain(g.get('relation', 'links'))} {plain(g.get('total', 0))}" for g in groups)
        lines.append(f"- Linked evidence: {counts}.")
        items = [
            i
            for g in groups
            for i in g.get("items", [])
            if i.get("kind") == "note" and NOTE.fullmatch(str(i.get("ref")))
        ]
        newest = sorted(items, key=lambda i: str(i.get("time", "")), reverse=True)[:NEWEST]
        lines.extend(f"  - {day(i.get('time'))} {plain(i.get('title', ''))} ({code(i['ref'])})" for i in newest)
    lines.append(f"Read more with {code('bf read ' + (note or repo))}; collected records are counted, not quoted.")
    return lines


def main(argv: list[str]) -> int:
    brain = argv[1] if len(argv) > 1 else ""
    repo = identity()
    deadline = time.monotonic() + LOOKUP_SECONDS
    # One read: a large note's first page still carries its ref and backlinks, all this hook prints.
    page = read(repo, brain, 0, deadline - time.monotonic()) if repo else None
    if not page or page.get("problems") or page.get("stale"):
        return 0
    ref = str(page.get("ref", ""))
    project = None
    if NOTE.fullmatch(ref) and ref.startswith("projects/"):
        offset = 0
        for _ in range(MAX_PAGES):
            listing = read("projects", brain, offset, deadline - time.monotonic())
            if not listing or listing.get("problems") or listing.get("stale"):
                return 0
            # Items name their brain only when several are selected; otherwise they share the page's brain.
            project = next(
                (
                    p
                    for p in listing.get("items", [])
                    if (p.get("brain", page.get("brain")), p.get("ref")) == (page.get("brain"), ref)
                ),
                None,
            )
            if project is not None or "next_offset" not in listing:
                break
            following = listing["next_offset"]
            if type(following) is not int or not offset < following < 2**63:
                return 0
            offset = following
        else:
            return 0
    sys.stdout.write("\n".join(render(repo, page, project)) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # A malformed reply must never block a session: print nothing and succeed.
        sys.exit(0)
