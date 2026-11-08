#!/usr/bin/env python3
"""Harness wrapper: add arguments only to runs that answer a message; no model involved.

Usage: harness.py [--input-arg ARG]... -- HARNESS [ARG...]

An assistant's `command` puts it in front of the agent harness. It replaces itself with HARNESS and its arguments,
adding each --input-arg value at the end only when BF_TRIGGER is `input`: a run that answers a message the channel
adapter admitted. Any other or missing value, such as `schedule`, `wake` or `manual`, adds nothing, so a run started by
a clock or by evidence keeps the narrower arguments.

It runs no shell and reads nothing: the harness receives the prompt on standard input, and BF's time and output bounds
apply to it as to any harness. Invalid arguments exit 2 and a harness that cannot start exits 1; neither error quotes
an argument.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn

USAGE = "usage: harness.py [--input-arg ARG]... -- HARNESS [ARG...]"
# BF sets BF_TRIGGER for its harness and never passes a caller's own value through.
INPUT = "input"


def fail(message: str, code: int) -> NoReturn:
    """Stop with one diagnostic that quotes no argument."""
    sys.stderr.write(f"harness: {message}\n")
    raise SystemExit(code)


def parse(argv: list[str]) -> tuple[list[str], list[str]]:
    """The --input-arg values and the harness argv. Each value is taken verbatim, even one that starts with a dash."""
    extra: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == "--":
            harness = argv[index + 1 :]
            if not harness or not harness[0]:
                fail(f"name the harness command after --; {USAGE}", 2)
            return extra, harness
        if argv[index] != "--input-arg" or index + 1 == len(argv):
            fail(USAGE, 2)
        extra.append(argv[index + 1])
        index += 2
    fail(f"put -- before the harness command; {USAGE}", 2)


def main(argv: list[str]) -> NoReturn:
    extra, harness = parse(argv)
    command = [*harness, *extra] if os.environ.get("BF_TRIGGER") == INPUT else harness
    try:
        # The harness takes this process's place, so BF's timeout and process-group kill reach it directly.
        os.execvp(command[0], command)  # noqa: S606 - configured argv, never a shell
    except OSError as error:
        fail(f"cannot start the harness command ({error.strerror or type(error).__name__}); check its name and PATH", 1)


if __name__ == "__main__":
    main(sys.argv[1:])
