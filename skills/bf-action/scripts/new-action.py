#!/usr/bin/env python3
"""Create an independent action with a unique path; never replace another session."""

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", help="Lowercase topic with hyphens")
    parser.add_argument("--brain", required=True, type=Path, help="Existing brain directory, not a registered name")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", args.topic) or len(args.topic) > 64:
        parser.error("topic must be a lowercase hyphenated slug of at most 64 characters")
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
                folder = f"{today}_{args.topic}-{uuid4().hex}"
                # Exclusive creation is the collision guard; a failure leaves existing work untouched.
                os.mkdir(folder, mode=0o700, dir_fd=actions)
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
