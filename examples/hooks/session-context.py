#!/usr/bin/env python3
"""Print the brain's context for the current repository at agent session start; silent when unavailable."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import _bf

REPLY_BYTES = _bf.MAX_REPLY
REMOTE_BYTES = 8 << 10
NEWEST = 3
ATTENTION = 5
# One lookup budget and page bound cover the repository read, the project listing and the home page.
LOOKUP_SECONDS = 20
MAX_PAGES = _bf.MAX_PAGES
ATTENTION_KINDS = frozenset({"sensor", "routine", "assistant", "channel"})
# Only authored note refs are printed, in code spans; a record ref is provider-controlled text and stays out.
NOTE = re.compile(
    r"(?:bf://[a-z][a-z0-9-]{0,63}/)?(?:projects|concepts|actions)/[^\x00-\x1f\x7f-\x9f]+?\.md(?:#[^\x00-\x1f\x7f-\x9f]*)?"
)
repository_identity = _bf.git_remote_identity
visible = _bf.visible
code = _bf.code


def plain(value: object, limit: int = 120) -> str:
    return _bf.plain(value, limit)


def identity() -> str:
    """The working directory's repository, as the Git history sensor records it; never raw remote credentials."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode or len(result.stdout) > REMOTE_BYTES:
        return ""
    return repository_identity(result.stdout.decode(errors="replace"))


def read(ref: str, brain: str, offset: int = 0, timeout: float = LOOKUP_SECONDS) -> dict[str, Any] | None:
    """One usable page: tolerate only stale-cache identity problems, with a visible notice at rendering."""
    args = [
        "read",
        *([ref] if ref else []),
        *(["--brain", brain] if brain else []),
        *(["--offset", str(offset)] if offset else []),
    ]
    try:
        return _bf.bf_json(*args, timeout=timeout, allow_stale=True, max_bytes=REPLY_BYTES)
    except _bf.BfError:
        return None


def day(value: object) -> str:
    """A note's date as written, or the local date of a datetime; empty when neither."""
    text = str(value or "")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text).isoformat()
        return datetime.fromisoformat(text).astimezone().date().isoformat()
    except ValueError:
        return ""


def address(item: dict[str, Any]) -> str:
    """With several selected brains, items carry a portable `uri`: their plain ref may name a note in each."""
    return str(item.get("uri") or item.get("ref", ""))


def owner(page: dict[str, Any]) -> str:
    """The exact read's `bf://` address, encoded as bf encodes one: it resolves whichever brains are selected."""
    ref = str(page.get("ref", ""))
    # As in bf, a ref ending in `.md` is a whole note, whose filename may hold a `#`.
    path, _, fragment = (ref, "", "") if ref.endswith(".md") or "#" not in ref else ref.rpartition("#")
    section = "#" + quote(fragment, safe="") if fragment else ""
    uri = f"bf://{page.get('brain', '')}/{quote(path, safe='/@:')}{section}"
    return uri if NOTE.fullmatch(uri) else ""


def render(
    repo: str, page: dict[str, Any], project: dict[str, Any] | None, attention: list[Any], stale: bool = False
) -> list[str]:
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
        # A note linking with several relations appears in each of their groups: list it once.
        items = {
            address(i): i
            for g in groups
            for i in g.get("items", [])
            if i.get("kind") == "note" and NOTE.fullmatch(address(i))
        }
        newest = sorted(items.values(), key=lambda i: day(i.get("date")), reverse=True)[:NEWEST]
        lines.extend(
            f"  - {' '.join(filter(None, [day(i.get('date')), plain(i.get('title', ''))]))} ({code(address(i))})"
            for i in newest
        )
    # Scheduled sensors, routines, assistants and channels that failed, are late, fell silent or keep missing parts:
    # their evidence may be older or sparser than it looks.
    late = [
        f"{plain(entry['name'], 64)} "
        + (
            "failed"
            if entry.get("failed")
            else "silent"
            if entry.get("silent") and entry.get("freshness") == "fresh"
            else "degraded"
            if entry.get("degraded") and entry.get("freshness") == "fresh"
            else plain(entry.get("freshness"), 16)
        )
        for entry in attention
        if isinstance(entry, dict)
        and entry.get("kind") in ATTENTION_KINDS
        and isinstance(entry.get("name"), str)
        and entry["name"]
    ]
    if late:
        extra = f" and {len(late) - ATTENTION} more" if len(late) > ATTENTION else ""
        lines.append(f"- Collection needs attention: {', '.join(late[:ATTENTION])}{extra}; see `bf status`.")
    if stale:
        lines.append("- Index refreshing: may miss the newest collection.")
    lines.append(f"Read more with {code('bf read ' + target)}; collected records are counted, not quoted.")
    return lines


def main(argv: list[str]) -> int:
    brain = argv[1] if len(argv) > 1 else ""
    repo = identity()
    deadline = time.monotonic() + LOOKUP_SECONDS
    # One read: a large note's first page still carries its ref and backlinks, all this hook prints.
    page = read(repo, brain, 0, deadline - time.monotonic()) if repo else None
    # An empty cache generation resolves no context worth printing.
    if not page or (page.get("stale") and not (page.get("ref") or page.get("backlinks"))):
        return 0
    stale = bool(page.get("stale"))
    ref = str(page.get("ref", ""))
    project = None
    if NOTE.fullmatch(ref) and ref.startswith("projects/"):
        try:
            for listing in _bf.bf_pages(
                "projects",
                brain=brain or None,
                deadline=deadline,
                allow_stale=True,
                max_pages=MAX_PAGES,
                max_bytes=REPLY_BYTES,
            ):
                stale = stale or bool(listing.get("stale"))
                # Items name their brain only when several are selected; otherwise they share the page's brain.
                project = next(
                    (
                        p
                        for p in listing.get("items", [])
                        if (p.get("brain", page.get("brain")), p.get("ref")) == (page.get("brain"), ref)
                    ),
                    None,
                )
                if project is not None:
                    break
        except _bf.BfError:
            return 0
    home = read("", brain, 0, deadline - time.monotonic())
    if not home or not isinstance(home.get("attention", []), list):
        return 0
    stale = stale or bool(home.get("stale"))
    sys.stdout.write("\n".join(render(repo, page, project, home.get("attention", []), stale)) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # A malformed reply must never block a session: print nothing and succeed
        sys.exit(0)
