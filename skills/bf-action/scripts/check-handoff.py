#!/usr/bin/env python3
"""Check an existing action's handoff budgets without printing or changing its text."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import cast

# Skills use the host's python3; this helper needs only the Python 3.11 standard library.
MAX_REPLY = 4 << 20  # Bytes of one bf reply.
ACTION = re.compile(r"(?:bf://([a-z][a-z0-9-]{0,63})/)?(actions/\d{4}-\d{2}-\d{2}_[a-z0-9-]+/ACTION\.md)")


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read(action: str, brain: str, section: str) -> tuple[str, str | int]:
    """The section's brain and text, or its length in characters when bf pages it."""
    ref = f"{action}#{section}"
    try:
        # bf already bounds replies. Capture its stdout only; diagnostics may contain private paths.
        result = subprocess.run(  # noqa: S603 - literal argv; bf never executes providers on read
            ["bf", "read", ref, "--brain", brain],  # noqa: S607 - use the host's selected bf executable
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if result.returncode or len(result.stdout) > MAX_REPLY:
            raise ValueError("read failed or exceeded the reply limit")
        reply = json.loads(result.stdout, object_pairs_hook=unique)
        if not isinstance(reply, dict) or reply.get("problems") or reply.get("stale"):
            raise ValueError("incomplete read")
        paged = "next_offset" in reply
        size = reply.get("total_characters")
        if reply.get("offset", 0) != 0 or (paged and type(size) is not int):
            raise ValueError("unexpected page")
        if any(key in reply for key in ("page", "record")):
            raise ValueError("expected an exact section")
        match = ACTION.fullmatch(action)
        assert match is not None  # noqa: S101 - argument validation happens before any read
        owner, path = match.groups()
        name, text = reply.get("brain"), reply.get("text")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError("expected a named brain")
        if (owner and owner != name) or reply.get("ref") != f"{path}#{section}":
            raise ValueError("read resolved to another action")
        if paged:
            return name, cast("int", size)
        if not isinstance(text, str) or not text.partition("\n")[2].strip():
            raise ValueError("empty section")
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, RecursionError) as error:
        raise ValueError(f"{section} is unavailable or incomplete; inspect its exact bf read") from error
    return name, text


def check(action: str, brain: str) -> dict[str, object]:
    sections: dict[str, object] = {}
    owner = ""
    passed = True
    for section, words_limit in (("context", 300), ("resume", 100)):
        limits = {"words": words_limit, **({"bytes": 4096} if section == "context" else {})}
        try:
            name, text = read(action, brain, section)
            if owner and owner != name:
                raise ValueError("sections resolved to different brains; use a brain-qualified action ref")
            owner = name
            # bf pages a section above 32 KiB, far beyond a handoff budget: fail it without reading the rest.
            counts = (
                {"characters": text}
                if isinstance(text, int)
                else {"words": len(text.split()), "bytes": len(text.encode("utf-8"))}
            )
        except (ValueError, UnicodeError) as error:
            # Only our fixed diagnostics leave the helper; never quote decoder or provider failures.
            message = (
                str(error)
                if isinstance(error, ValueError) and not isinstance(error, UnicodeError)
                else "invalid section encoding"
            )
            return {"checked": False, "passed": False, "sections": sections, "error": message}
        okay = "characters" not in counts and all(counts[key] <= limit for key, limit in limits.items())
        path = action.removeprefix(f"bf://{name}/")
        sections[section] = {"ref": f"bf://{name}/{path}#{section}", **counts, "limits": limits, "passed": okay}
        passed = passed and okay
    return {"checked": True, "passed": passed, "sections": sections}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", help="actions/YYYY-MM-DD_topic-SUFFIX/ACTION.md, optionally brain-qualified")
    parser.add_argument("--brain", required=True, help="brain name or path passed to bf read")
    parser.add_argument("--hook", action="store_true", help="emit a non-blocking Claude Code SessionStart reply")
    args = parser.parse_args()
    if len(args.action) > 1024 or not ACTION.fullmatch(args.action):
        parser.error("expected an action entry ref without a section")
    report = check(args.action, args.brain)
    output: dict[str, object] = report
    if args.hook:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "Brain handoff size check (evidence, not instructions): "
                + json.dumps(report, separators=(",", ":")),
            }
        }
    sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
    return 0 if args.hook or cast("bool", report["passed"]) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
