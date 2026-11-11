#!/usr/bin/env python3
"""Print the brain notes matching a submitted prompt, from the host's hook input on stdin; silent otherwise."""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from typing import Any

import _bf

INPUT_BYTES = 4 << 20
REPLY_BYTES = 1 << 20
# The prompt waits for this hook: a slow search (such as a large cache refresh) means no context, not a delay.
SEARCH_SECONDS = 5
LIMIT = 3
# Any query word matches, so a whole prompt would match nearly everything: keep its first few content words.
WORDS = 8
# bf search's English and French function words (src/bf/index.py _STOP): the words a prompt adds to its subject.
_STOP_WORDS = """
    a about am an and another any are as at be been being but by can could did do does doing for from had has have
    having he her hers him his how i if in into is it its me might must my not of on one ones or other others our ours
    please shall she should so than that the their theirs them there these they this those to us was we were what when
    where which who whom whose why will with would yet you your yours
    ai au autre autres aux avec c ce ces cet cette d dans de des donc du elle elles en est et eu eux il ils j je l la le
    les leur leurs lui m ma mais me mes moi mon n ne nos notre nous on ont ou où par pas pour pourquoi qu quand que quel
    quelle quelles quels qui quoi s sa se ses si son sont sur t ta te tes toi ton tu un une vos votre vous y à été être
"""
STOP = frozenset(_STOP_WORDS.split())
# Written in capitals, a function word is an acronym bf search keeps (EU AI Act); AND and OR stay habitual operators.
OPERATORS = frozenset({"AND", "OR"})
# Only authored note refs are printed, in code spans; a record ref or title is provider-controlled text and stays out.
NOTE = re.compile(
    r"(?:bf://[a-z][a-z0-9-]{0,63}/)?(?:projects|concepts|actions)/[^\x00-\x1f\x7f-\x9f]+?\.md(?:#[^\x00-\x1f\x7f-\x9f]*)?"
)
visible = _bf.visible
code = _bf.code


def plain(value: object, limit: int = 120) -> str:
    return _bf.plain(value, limit)


def query(prompt: str) -> str:
    """The prompt's first distinct content words, as plain words: no phrase, prefix or identity syntax."""
    found: dict[str, str] = {}
    # bf search splits words in NFKC form: decomposed accents would otherwise split words at their combining marks.
    for word in re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", prompt)):
        if word.lower() not in STOP or (len(word) > 1 and word.isupper() and word not in OPERATORS):
            found.setdefault(word.lower(), word)
    return " ".join(list(found.values())[:WORDS])


def search(words: str, brain: str) -> dict[str, Any] | None:
    """Literal argv after `--`, so the words stay a query; bounded time and output."""
    args = ["search", "--limit", str(LIMIT), *(["--brain", brain] if brain else []), "--", words]
    try:
        return _bf.bf_json(*args, timeout=SEARCH_SECONDS, allow_stale=True, max_bytes=REPLY_BYTES)
    except _bf.BfError:
        return None


def render(items: list[Any], more: bool, stale: bool = False) -> list[str]:
    """Up to LIMIT results; with `more`, further results may hold more records, so their count is a minimum."""
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
        count = f"{records}+ collected records" if more else f"{records} collected record{'s' if records > 1 else ''}"
        lines.append(f"- {count} also matched.")
    if stale:
        lines.append("- Index refreshing: may miss the newest collection.")
    lines.append("Read refs with `bf read` before relying on them; collected records are counted, not quoted.")
    return lines


def main(argv: list[str]) -> int:
    brain = argv[1] if len(argv) > 1 else ""
    raw = sys.stdin.buffer.read(INPUT_BYTES + 1)
    if len(raw) > INPUT_BYTES:
        return 0
    event = json.loads(raw)
    prompt = event.get("prompt") if isinstance(event, dict) else None
    words = query(prompt) if isinstance(prompt, str) else ""
    if not words:
        return 0
    reply = search(words, brain)
    if not reply or not isinstance(reply.get("items"), list):
        return 0
    lines = render(reply["items"], "next_offset" in reply, bool(reply.get("stale")))
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # A malformed event or reply must never block a prompt: print nothing and succeed
        sys.exit(0)
