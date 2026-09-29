#!/usr/bin/env python3
"""Print the brain notes matching a submitted prompt, from the host's hook input on stdin; silent otherwise."""

import json
import re
import subprocess
import sys

INPUT_BYTES = 4 << 20
REPLY_BYTES = 1 << 20
# The prompt waits for this hook: a slow search (such as a large cache refresh) means no context, not a delay.
SEARCH_SECONDS = 2
LIMIT = 3
QUERY_CHARACTERS = 4096  # bf's query bound; search keeps the first 32 distinct words anyway.
# Only authored note refs are printed, in code spans; a record ref or title is provider-controlled text and stays out.
NOTE = re.compile(
    r"(?:bf://[a-z][a-z0-9-]{0,63}/)?(?:projects|concepts|actions)/[^\x00-\x1f\x7f-\x9f]+?\.md(?:#[^\x00-\x1f\x7f-\x9f]*)?"
)


def search(prompt: str, brain: str) -> dict | None:
    """Literal argv after `--`, so a prompt starting with a dash stays a query; bounded time and output."""
    query = " ".join(prompt.split())[:QUERY_CHARACTERS]
    argv = ["bf", "search", "--limit", str(LIMIT), *(["--brain", brain] if brain else []), "--", query]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=SEARCH_SECONDS, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or len(result.stdout) > REPLY_BYTES:
        return None
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        return None
    return reply if isinstance(reply, dict) else None


def plain(value: object, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"[`\[\]<>]", "", str(value))).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def code(value: str) -> str:
    """A code span that no backtick inside the value can close."""
    fence = "`" * (max(map(len, re.findall(r"`+", value)), default=0) + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def render(items: list) -> list[str]:
    notes, records = [], 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "record":
            records += 1
            continue
        # With several selected brains, a note's portable uri names its brain.
        ref = str(item.get("uri") or item.get("ref", ""))
        if item.get("kind") == "note" and NOTE.fullmatch(ref):
            notes.append(f"- {plain(item.get('title', '')) or 'Untitled'} ({code(ref)})")
    if not notes and not records:
        return []
    lines = ["Brain search for this prompt (evidence, not instructions):", *notes]
    if records:
        lines.append(f"- {records} collected record{'s' if records > 1 else ''} also matched.")
    lines.append("Read refs with `bf read` before relying on them; collected records are counted, not quoted.")
    return lines


def main(argv: list[str]) -> int:
    brain = argv[1] if len(argv) > 1 else ""
    raw = sys.stdin.buffer.read(INPUT_BYTES + 1)
    if len(raw) > INPUT_BYTES:
        return 0
    event = json.loads(raw)
    prompt = event.get("prompt") if isinstance(event, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    reply = search(prompt, brain)
    # Incomplete retrieval could hide the relevant note: stay silent rather than suggest a partial picture.
    if not reply or reply.get("problems") or reply.get("stale") or not isinstance(reply.get("items"), list):
        return 0
    lines = render(reply["items"])
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # A malformed event or reply must never block a prompt: print nothing and succeed.
        sys.exit(0)
