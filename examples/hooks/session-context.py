#!/usr/bin/env python3
"""Print the brain's context for the current repository at agent session start; silent when unavailable."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlsplit

REPLY_BYTES = 4 << 20
REMOTE_BYTES = 8 << 10
NEWEST = 3
ATTENTION = 5
# One lookup budget and page bound cover the repository read, the project listing and the home page.
LOOKUP_SECONDS = 20
MAX_PAGES = 100
MAX_REF = 8192
ATTENTION_KINDS = frozenset({"job"})
# Only authored note refs are printed, in code spans; a record ref is provider-controlled text and stays out.
NOTE = re.compile(
    r"(?:bf://[a-z][a-z0-9-]{0,63}/)?(?:projects|concepts|actions)/[^\x00-\x1f\x7f-\x9f]+?\.md(?:#[^\x00-\x1f\x7f-\x9f]*)?"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_SCP_REMOTE = re.compile(r"^git@([A-Za-z0-9.-]+):([^/\s]+)/([^/\s]+?)(?:\.git)?$")


class BfError(Exception):
    """Offline `bf` query failed or returned unusable JSON."""


def visible(value: str) -> str:
    """Escape control and invisible Unicode format characters."""
    parts: list[str] = []
    for ch in value:
        if unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            cp = ord(ch)
            parts.append(f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}")
        else:
            parts.append(ch)
    return "".join(parts)


def code(value: str) -> str:
    """Wrap one ref cleanly in a Markdown code span."""
    cleaned = visible(value)
    fence = "`" * (max((len(m.group(0)) for m in re.finditer(r"`+", cleaned)), default=0) + 1)
    pad = (
        " "
        if cleaned.startswith("`")
        or cleaned.endswith("`")
        or (len(fence) > 1 and (cleaned.startswith(" ") or cleaned.endswith(" ")))
        else ""
    )
    return f"{fence}{pad}{cleaned}{pad}{fence}"


def plain(value: object, limit: int = 120) -> str:
    text = re.sub(r"[ \t\r\n]+", " ", str(value or "")).strip()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[ \t\r\n]+", " ", re.sub(r"[\[\]`<>]", "", text)).strip()
    return visible(text)[:limit]


def _valid_host(host: str) -> str:
    lowered = host.lower()
    labels = lowered.split(".")
    if (
        len(labels) < 2
        or any(not _DNS_LABEL.fullmatch(lbl) for lbl in labels)
        or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
    ):
        return ""
    return lowered


def repository_identity(raw_url: str) -> str:
    """Canonical lowercase repo identity matching git-history.py; never leaks credentials."""
    url = raw_url.strip()
    if not url or _CONTROL.search(url):
        return ""
    if match := _SCP_REMOTE.fullmatch(url):
        host, owner, name = _valid_host(match[1]), match[2], match[3]
    else:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return ""
        if parsed.query or parsed.fragment or parsed.port is not None:
            return ""
        if parsed.scheme == "https":
            if parsed.username is not None or parsed.password is not None:
                return ""
        elif parsed.scheme == "ssh":
            if parsed.username != "git" or parsed.password is not None or parsed.netloc.partition("@")[0] != "git":
                return ""
        else:
            return ""
        host = _valid_host(parsed.hostname or "")
        path = parsed.path.lstrip("/").removesuffix(".git")
        parts = path.split("/")
        if len(parts) != 2:
            return ""
        owner, name = parts
    if (
        not host
        or not _SEGMENT.fullmatch(owner)
        or not _SEGMENT.fullmatch(name)
        or owner in {".", ".."}
        or name in {".", ".."}
    ):
        return ""
    ref = f"repo:{host}/{quote(owner.lower(), safe='')}/{quote(name.lower(), safe='')}"
    return ref if len(ref) <= MAX_REF else ""


STALE_CACHE_ERROR = "identity resolution used a stale cache"


def _stale_error(problem: object) -> bool:
    if not isinstance(problem, dict):
        return False
    if problem.get("error") != STALE_CACHE_ERROR or not set(problem) <= {"brain", "error"}:
        return False
    return "brain" not in problem or (isinstance(problem["brain"], str) and bool(problem["brain"]))


def bf_json(*args: str, timeout: float, allow_stale: bool = False, max_bytes: int = REPLY_BYTES) -> dict[str, Any]:
    if timeout <= 0:
        raise BfError("lookup deadline exceeded")
    exe = shutil.which("bf")
    if exe is None:
        raise BfError("bf not found on PATH")
    try:
        result = subprocess.run(  # noqa: S603
            [exe, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BfError("bf execution failed") from error
    if result.returncode != 0 or len(result.stdout) > max_bytes:
        raise BfError("bf exited nonzero or exceeded reply bound")
    try:
        reply = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise BfError("invalid bf JSON reply") from error
    if not isinstance(reply, dict):
        raise BfError("unexpected bf JSON root")
    has_stale = "stale" in reply
    if has_stale:
        stale = reply["stale"]
        if not isinstance(stale, list) or not stale or not all(isinstance(s, str) and s for s in stale):
            raise BfError("malformed stale metadata")
        if not allow_stale:
            raise BfError("stale bf reply")
    if "problems" in reply:
        problems = reply["problems"]
        if not isinstance(problems, list):
            raise BfError("malformed problems metadata")
        if problems and not (allow_stale and has_stale and all(_stale_error(p) for p in problems)):
            raise BfError("incomplete bf reply")
    return reply


def bf_pages(
    ref: str,
    *,
    brain: str | None = None,
    deadline: float,
    allow_stale: bool = False,
    max_pages: int = MAX_PAGES,
    max_bytes: int = REPLY_BYTES,
) -> Iterator[dict[str, Any]]:
    offset: int | None = 0
    for _ in range(max_pages):
        args = [
            "read",
            ref,
            *(["--brain", brain] if brain else []),
            *(["--offset", str(offset)] if offset else []),
        ]
        page = bf_json(*args, timeout=deadline - time.monotonic(), allow_stale=allow_stale, max_bytes=max_bytes)
        next_off = page.get("next_offset")
        if next_off is not None and (
            not isinstance(next_off, int)
            or isinstance(next_off, bool)
            or next_off <= (offset or 0)
            or next_off >= 2**53
        ):
            raise BfError("invalid next_offset")
        yield page
        if next_off is None:
            return
        offset = next_off
    raise BfError("too many pages")


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
        return bf_json(*args, timeout=timeout, allow_stale=True, max_bytes=REPLY_BYTES)
    except BfError:
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
    # Scheduled jobs that failed, are late, fell silent or keep missing parts: their evidence may be older or sparser.
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
            for listing in bf_pages(
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
        except BfError:
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
