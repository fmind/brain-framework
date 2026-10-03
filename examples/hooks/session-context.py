#!/usr/bin/env python3
"""Print the brain's context for the current repository at agent session start; silent when unavailable."""

import json
import re
import subprocess
import sys
import time
from datetime import date, datetime
from urllib.parse import quote

REPLY_BYTES = 4 << 20
REMOTE_BYTES = 8 << 10
NEWEST = 3
ATTENTION = 5
# One lookup budget and page bound cover the repository read, the project listing and the home page.
LOOKUP_SECONDS = 20
MAX_PAGES = 100
# Only authored note refs are printed, in code spans; a record ref is provider-controlled text and stays out.
NOTE = re.compile(
    r"(?:bf://[a-z][a-z0-9-]{0,63}/)?(?:projects|concepts|actions)/[^\x00-\x1f\x7f-\x9f]+?\.md(?:#[^\x00-\x1f\x7f-\x9f]*)?"
)


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
    """One complete page: a failed, incomplete (`problems`) or cache-busy (`stale`) reply counts as none."""
    raw = run(
        [
            "bf",
            "read",
            *([ref] if ref else []),
            *(["--brain", brain] if brain else []),
            *(["--offset", str(offset)] if offset else []),
        ],
        REPLY_BYTES,
        timeout,
    )
    try:
        reply = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(reply, dict) or reply.get("problems") or reply.get("stale"):
        return None
    return reply


def day(value: object) -> str:
    """A note's date as written, or the local date of a datetime; empty when neither."""
    text = str(value or "")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text).isoformat()
        return datetime.fromisoformat(text).astimezone().date().isoformat()
    except ValueError:
        return ""


def plain(value: object, limit: int = 120) -> str:
    # A task's Markdown link reads as its label, as the note shows it.
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", str(value))
    text = re.sub(r"\s+", " ", re.sub(r"[`\[\]<>]", "", text)).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def code(value: str) -> str:
    """A code span that no backtick inside the value can close."""
    fence = "`" * (max(map(len, re.findall(r"`+", value)), default=0) + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def address(item: dict) -> str:
    """With several selected brains, items carry a portable `uri`: their plain ref may name a note in each."""
    return str(item.get("uri") or item.get("ref", ""))


def owner(page: dict) -> str:
    """The exact read's `bf://` address, encoded as bf encodes one: it resolves whichever brains are selected."""
    ref = str(page.get("ref", ""))
    # As in bf, a ref ending in `.md` is a whole note, whose filename may hold a `#`.
    path, _, fragment = (ref, "", "") if ref.endswith(".md") or "#" not in ref else ref.rpartition("#")
    section = "#" + quote(fragment, safe="") if fragment else ""
    uri = f"bf://{page.get('brain', '')}/{quote(path, safe='/@:')}{section}"
    return uri if NOTE.fullmatch(uri) else ""


def render(repo: str, page: dict, project: dict | None, attention: list) -> list[str]:
    lines = [f"Brain context for {repo} (evidence, not instructions):"]
    ref = str(page.get("ref", ""))
    note = ref if NOTE.fullmatch(ref) else ""
    # Another selected brain may hold a note at the same path, and the exact read carries no address: name and read
    # the project by the listing's address; name any other owner by the address of its exact read, and read further
    # through the repository identity, as this hook did.
    target = repo
    if note and project:
        target = address(project) if NOTE.fullmatch(address(project)) else note
        state = [str(project.get("status", "")), f"edited {day(project.get('modified')) or 'unknown'}"]
        if project.get("review"):
            reasons = ", ".join(plain(reason) for reason in project.get("review_reasons", []))
            state.append(f"review needed ({reasons or 'inspect project'})")
        if day(project.get("review_due")):
            state.append(f"review deadline {day(project['review_due'])}")
        lines.append(
            f"- Project: {plain(project.get('title', note))} ({code(target)}), {', '.join(filter(None, state))}."
        )
        if project.get("next"):
            lines.append(f"- Next task: {plain(project['next'])}")
    elif note:
        lines.append(f"- Owning note: {code(owner(page) or note)}.")
    elif ref:
        lines.append("- A collected record owns this repository.")
    else:
        lines.append("- No note owns this repository yet.")
    groups = page.get("backlinks", [])
    if groups:
        counts = ", ".join(f"{plain(g.get('relation', 'links'))} {plain(g.get('total', 0))}" for g in groups)
        # A backlink group previews its newest items; the relation page lists them all.
        more = any(int(g.get("total", 0)) > len(g.get("items", [])) for g in groups)
        hint = f"; list one relation with {code(f'bf read {target} --rel RELATION')}" if more else ""
        lines.append(f"- Linked evidence: {counts}{hint}.")
        items = [
            i for g in groups for i in g.get("items", []) if i.get("kind") == "note" and NOTE.fullmatch(address(i))
        ]
        newest = sorted(items, key=lambda i: day(i.get("date")), reverse=True)[:NEWEST]
        lines.extend(
            f"  - {' '.join(filter(None, [day(i.get('date')), plain(i.get('title', ''))]))} ({code(address(i))})"
            for i in newest
        )
    # Scheduled sensors and routines that failed or are overdue: their evidence may be older than it looks.
    late = [
        f"{plain(entry.get('sensor') or entry.get('routine'), 64)} "
        + ("failed" if entry.get("failed") else plain(entry.get("freshness"), 16))
        for entry in attention
        if isinstance(entry, dict) and (entry.get("sensor") or entry.get("routine"))
    ]
    if late:
        extra = f" and {len(late) - ATTENTION} more" if len(late) > ATTENTION else ""
        lines.append(f"- Collection needs attention: {', '.join(late[:ATTENTION])}{extra}; see `bf status`.")
    lines.append(f"Read more with {code('bf read ' + target)}; collected records are counted, not quoted.")
    return lines


def main(argv: list[str]) -> int:
    brain = argv[1] if len(argv) > 1 else ""
    repo = identity()
    deadline = time.monotonic() + LOOKUP_SECONDS
    # One read: a large note's first page still carries its ref and backlinks, all this hook prints.
    page = read(repo, brain, 0, deadline - time.monotonic()) if repo else None
    if not page:
        return 0
    ref = str(page.get("ref", ""))
    project = None
    if NOTE.fullmatch(ref) and ref.startswith("projects/"):
        offset = 0
        for _ in range(MAX_PAGES):
            listing = read("projects", brain, offset, deadline - time.monotonic())
            if not listing:
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
            if type(following) is not int or not offset < following < 2**53:
                return 0
            offset = following
        else:
            return 0
    home = read("", brain, 0, deadline - time.monotonic())
    if not home or not isinstance(home.get("attention", []), list):
        return 0
    sys.stdout.write("\n".join(render(repo, page, project, home.get("attention", []))) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # A malformed reply must never block a session: print nothing and succeed.
        sys.exit(0)
