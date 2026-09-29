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
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


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


def skeleton(topic: str, today: str) -> str:
    """The ACTION.md text: OKF metadata, a title and the template's headings."""
    title = topic.replace("-", " ").capitalize()
    return f"---\ntype: action\nstatus: draft\nupdated: {today}\n---\n\n# {title}\n\n" + "\n\n".join(SECTIONS) + "\n"


def write(actions: int, folder: str, text: str) -> None:
    """Write ACTION.md into the new folder; on failure, remove the file this call created."""
    action = os.open(folder, FLAGS, dir_fd=actions)
    try:
        entry = os.open("ACTION.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=action)
        try:
            with os.fdopen(entry, "w", encoding="utf-8") as stream:
                stream.write(text)
        except BaseException:
            with suppress(OSError):
                os.unlink("ACTION.md", dir_fd=action)
            raise
    finally:
        os.close(action)


def start(brain: Path, topic: str, *, unique: bool) -> str:
    """The new action's folder name; a failure removes only the folders this call created."""
    # Like bf, follow a linked brain root once; nothing below it is followed.
    root = os.open(brain, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config = os.open("bf.yaml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            if not stat.S_ISREG(os.fstat(config).st_mode):
                raise OSError("expected regular brain configuration")
        finally:
            os.close(config)
        made = False
        with suppress(FileExistsError):
            os.mkdir("actions", mode=0o700, dir_fd=root)
            made = True
        try:
            actions = os.open("actions", FLAGS, dir_fd=root)
            try:
                today = date.today().isoformat()
                folder = create(actions, topic, today, unique=unique)
                try:
                    write(actions, folder, skeleton(topic, today))
                except BaseException:
                    with suppress(OSError):
                        os.rmdir(folder, dir_fd=actions)
                    raise
            finally:
                os.close(actions)
        except BaseException:
            if made:
                # rmdir removes only an empty folder: never another session's work.
                with suppress(OSError):
                    os.rmdir("actions", dir_fd=root)
            raise
    finally:
        os.close(root)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("topic", help="lowercase words joined by hyphens, such as website-review")
    parser.add_argument("--brain", required=True, type=Path, help="the brain directory, not a registered name")
    parser.add_argument(
        "--unique", action="store_true", help="always add the 8-hex suffix, for a brain that several clones share"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", args.topic) or len(args.topic) > 64:
        parser.error("topic must be lowercase words joined by single hyphens, at most 64 characters")
    try:
        folder = start(args.brain.expanduser(), args.topic, unique=args.unique)
    except OSError:
        parser.exit(1, "Could not create an action safely; inspect permissions and existing paths, then retry.\n")
    sys.stdout.write(json.dumps({"action": f"actions/{folder}/ACTION.md"}) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
