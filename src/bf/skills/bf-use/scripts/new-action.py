#!/usr/bin/env python3
"""Create a dated action folder with an ACTION.md skeleton; never replace or join an existing action.

The folder is actions/YYYY-MM-DD_TOPIC. When that folder exists, or with --unique, it gains an 8-hex suffix,
as routine actions do, so sessions written by several clones of a shared brain never share a folder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from contextlib import suppress
from datetime import date
from pathlib import Path
from uuid import uuid4

# The headings of templates/action.md, without its sample text; tests keep the two in sync.
SECTIONS = ("## Context {#context}", "## TODO", "## Decision {#decision}", "## Resume {#resume}", "## Outcome")
# A suffixed name that also exists is astronomically unlikely; a few attempts end a pathological loop.
ATTEMPTS = 5


def create(actions: int, topic: str, today: str, *, unique: bool) -> str:
    """The folder created exclusively below `actions`: exclusive creation is the collision guard."""
    names = ([] if unique else [f"{today}_{topic}"]) + [f"{today}_{topic}-{uuid4().hex[-8:]}" for _ in range(ATTEMPTS)]
    for name in names:
        try:
            os.mkdir(name, mode=0o700, dir_fd=actions)
        except FileExistsError:
            continue
        return name
    raise FileExistsError(topic)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("topic", help="lowercase words joined by hyphens, such as website-review")
    parser.add_argument("--brain", required=True, type=Path, help="the brain directory, not a registered name")
    parser.add_argument(
        "--unique", action="store_true", help="always add the 8-hex suffix, for a brain that several clones share"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", args.topic) or len(args.topic) > 64:
        parser.error("topic must be lowercase words joined by single hyphens, at most 64 characters")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        # Like bf, follow a linked brain root once; nothing below it is followed.
        root = os.open(args.brain.expanduser(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            config = os.open("bf.yaml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            try:
                if not stat.S_ISREG(os.fstat(config).st_mode):
                    raise OSError("expected regular brain configuration")
            finally:
                os.close(config)
            with suppress(FileExistsError):
                os.mkdir("actions", mode=0o700, dir_fd=root)
            actions = os.open("actions", flags, dir_fd=root)
            try:
                today = date.today().isoformat()
                folder = create(actions, args.topic, today, unique=args.unique)
                action = os.open(folder, flags, dir_fd=actions)
                try:
                    entry = os.open(
                        "ACTION.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=action
                    )
                    with os.fdopen(entry, "w", encoding="utf-8") as stream:
                        title = args.topic.replace("-", " ").capitalize()
                        stream.write(
                            f"---\ntype: action\nstatus: draft\nupdated: {today}\n---\n\n# {title}\n\n"
                            + "\n\n".join(SECTIONS)
                            + "\n"
                        )
                finally:
                    os.close(action)
            finally:
                os.close(actions)
        finally:
            os.close(root)
    except OSError:
        parser.exit(1, "Could not create an action safely; inspect permissions and existing paths, then retry.\n")
    sys.stdout.write(json.dumps({"action": f"actions/{folder}/ACTION.md"}) + "\n")


if __name__ == "__main__":
    main()
