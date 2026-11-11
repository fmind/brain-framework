#!/usr/bin/env python3
"""Records gate: report each record a source gained or changed during a schedule's window, once; no model involved.

Usage: records-gate.py SOURCE [SOURCE...] [--where FIELD=VALUE]... [--period PERIOD]

An assistant's schedule runs it as its gate. For each SOURCE, it streams `bf export --kind items --scope
memories/SOURCE` from the first `bf` on PATH and keeps the records whose `observed` time, when BF first collected or
last changed them, falls from 10 minutes before BF_START until BF_END. `--where` keeps records whose single-value
field, as the export lists it, reads VALUE; repeated, every condition must hold. It prints `{"events": [...]}`, one
event per record named by its `bf://BRAIN/REF` address, and remembers the reported records in BF_STATE_DIR so the
harness hears of each change once. The export also lists the brains this brain references: their records count too.

Any failure prints one line on stderr that quotes no record, exits 1 and reports nothing, so BF retries the gate.
Invalid arguments or a missing BF_START, BF_END or BF_STATE_DIR exit 2.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from collections.abc import Generator, Mapping
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

import _bf

# `observed` is stamped when a collection starts, but its records reach the cache when it ends: a collection still
# running when the previous check exported shows its records later, dated before this window. Reach back to them;
# the remembered keys stop a repeat.
SLACK = timedelta(minutes=10)
# Reported keys are forgotten after 35 days, so a schedule whose window restarts reports nothing twice in that time.
KEEP = timedelta(days=35)
STATE = "records-gate.json"
TEXT_CHARS = 280
MAX_SOURCES = 32
# Below the gate's default 120-second timeout, so the gate can still report why it stopped; a lower gate `timeout` stops
# the gate and its export first. A whole-source export reads every record of the source: see the README for a measured
# time.
TIMEOUT = 100
LINE_BYTES = _bf.LINE_BYTES
STATE_BYTES = _bf.MAX_STATE


def fail(message: str, code: int = 1) -> NoReturn:
    """Stop with one diagnostic that names the failure and quotes no record."""
    _bf.fail(message, code, prefix="records-gate")


def instant(value: object) -> datetime | None:
    """An aware timestamp, or None when the value is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def condition(value: str) -> tuple[str, str]:
    """A `--where FIELD=VALUE` pair: a BF field name, then any value."""
    field, separator, expected = value.partition("=")
    if not separator or not _bf.NAME.fullmatch(field):
        raise argparse.ArgumentTypeError("expected FIELD=VALUE with a field name such as kind")
    return field, expected


def source(value: str) -> str:
    if not _bf.NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a source name such as mail")
    return value


def period(value: str) -> str:
    if not value or "/" in value or value != value.strip():
        raise argparse.ArgumentTypeError("expected one period such as 7d or 2026-09")
    return value


def window() -> tuple[datetime, datetime, Path]:
    """BF_START, BF_END and BF_STATE_DIR, which BF sets for a gate; run by hand, set them yourself."""
    if not os.environ.get("BF_START") or not os.environ.get("BF_END") or not os.environ.get("BF_STATE_DIR"):
        fail("BF_START, BF_END and BF_STATE_DIR must be set; BF sets them when it runs a gate", 2)
    try:
        start, end = _bf.window()
        folder = _bf.state_dir()
    except _bf.BfError:
        fail("BF_START, BF_END and BF_STATE_DIR must be set; BF sets them when it runs a gate", 2)
    return start, end, folder


def export(bf: str, scope: str, deadline: float) -> Generator[dict[str, Any], None, None]:
    """Stream each line of `bf export --kind items --scope SCOPE`, failing on any sign of incomplete evidence."""
    try:
        yield from _bf.bf_export(
            scope,
            kind="items",
            timeout=TIMEOUT,
            deadline=deadline,
            max_line_bytes=LINE_BYTES,
            executable=bf,
        )
    except _bf.BfError as error:
        message = str(error)
        if f"ran past the {TIMEOUT}-second limit" in message:
            message = message.replace(
                f"ran past the {TIMEOUT}-second limit",
                f"ran past the gate's {TIMEOUT}-second limit; narrow it with --period",
            )
        fail(message, error.status)


def address(brain: str, ref: str) -> str:
    """A record's address, spelled as a search result's `uri`: a referenced brain can hold the same ref."""
    return f"bf://{brain}/{quote(ref, safe='/@:')}"


def spelled(value: object) -> str | None:
    """A field value as `--where` spells it: text as is, true or false, a number as JSON writes it."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    return None


def matches(record: dict[str, Any], where: list[tuple[str, str]]) -> bool:
    """Whether every condition holds. The export lists declared single-value fields of up to 200 characters only: a
    `cardinality: many` field never matches."""
    fields = record.get("fields", {})
    return isinstance(fields, dict) and all(spelled(fields.get(field)) == expected for field, expected in where)


def line(title: object) -> str:
    """A record title as event text: one line of at most TEXT_CHARS characters, hidden characters made visible."""
    text = " ".join(str(title).split()) or "untitled"
    return _bf.visible(text if len(text) <= TEXT_CHARS else text[: TEXT_CHARS - 1] + "…")


def encoded(seen: set[str]) -> bytes:
    return json.dumps({"seen": sorted(seen)}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def bounded(seen: set[str]) -> set[str]:
    """The keys the state file can hold within STATE_BYTES, newest first, so load() can read it back."""
    if len(encoded(seen)) <= STATE_BYTES:
        return seen
    kept: set[str] = set()
    used = len(encoded(set()))
    for key in sorted(seen, key=lambda key: key.rpartition("@")[2], reverse=True):
        used += len(json.dumps(key, ensure_ascii=False).encode()) + 1
        if used > STATE_BYTES:
            break
        kept.add(key)
    dropped = f"{len(seen) - len(kept)} of {len(seen)}"
    sys.stderr.write(
        f"records-gate: forgot the oldest reported records, {dropped}, to keep {STATE} within {STATE_BYTES} bytes\n"
    )
    return kept


def load(path: Path) -> set[str]:
    """The keys already reported; a missing file reports everything in the window, a damaged one stops the gate."""
    try:
        state = _bf.load_state(path.absolute(), {"seen": []}, max_bytes=STATE_BYTES)
    except _bf.BfError:
        state = None
    seen = state.get("seen") if isinstance(state, dict) else None
    if not isinstance(seen, list) or not all(isinstance(key, str) for key in seen):
        fail(f"cannot read {STATE} in BF_STATE_DIR; delete it to report this window's records again")
    return set(seen)


def save(path: Path, seen: set[str]) -> None:
    """Replace the state file whole, so an interrupted write keeps the previous one."""
    kept = bounded(seen)
    try:
        _bf.save_state(path.absolute(), {"seen": sorted(kept)}, max_bytes=STATE_BYTES)
    except _bf.BfError as error:
        fail(str(error), error.status)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="+", type=source, metavar="SOURCE", help="a source name, such as mail")
    parser.add_argument("--where", action="append", default=[], type=condition, metavar="FIELD=VALUE")
    parser.add_argument("--period", type=period, help="export only this period of each source, such as 7d")
    args = parser.parse_args(argv)
    sources = list(dict.fromkeys(args.sources))
    if len(sources) > MAX_SOURCES:
        parser.error(f"at most {MAX_SOURCES} sources")
    start, end, folder = window()
    bf = shutil.which("bf")
    if bf is None:
        fail("bf is not on PATH; install bf 21.3.0 or later")
    seen = load(folder / STATE)
    deadline = time.monotonic() + TIMEOUT
    # Each record version found in the window: its observed time, address, source and title.
    found: list[tuple[str, str, str, str]] = []
    for name in sources:
        scope = f"memories/{name}/{args.period}" if args.period else f"memories/{name}"
        with closing(export(bf, scope, deadline)) as items:
            for number, record in enumerate(items, 1):
                if record.get("kind") != "record" or "observed" not in record:
                    # A record placed by hand has no collection time: nothing says when it arrived.
                    continue
                observed = instant(record["observed"])
                if observed is None:
                    fail(f"bf export of {scope} printed an invalid observed time on line {number}")
                if start - SLACK <= observed < end and matches(record, args.where):
                    stamp, uri = _bf.canonical_time(observed), address(record["brain"], record["ref"])
                    if f"{uri}@{stamp}" not in seen:
                        found.append((stamp, uri, name, line(record.get("title", ""))))
    # The newest changes first: the overflow event counts the older ones.
    found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    candidates = [
        {"key": f"{uri}@{stamp}", "text": text, "ref": uri, "group": name} for stamp, uri, name, text in found
    ]

    def summary(dropped: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        left = Counter(str(item["group"]) for item in dropped)
        counts = ", ".join(f"{name} {count}" for name, count in sorted(left.items()))
        single = next(iter(left)) if len(left) == 1 else ""
        scope = f"memories/{single}/{args.period}" if single and args.period else f"memories/{single}"
        return {
            "key": f"{single}:more" if single else "records:more",
            "text": f"{left.total()} more new or changed records are not listed: {counts}. Read their source pages.",
            **({"ref": scope} if single else {}),
        }

    current = {f"{uri}@{stamp}" for stamp, uri, *_ in found}
    horizon = end - KEEP
    kept = {key for key in seen if (moment := instant(key.rpartition("@")[2])) is not None and moment >= horizon}
    # Saved before printing: a gate that printed but could not remember would report the same records again.
    if current or kept != seen:
        save(folder / STATE, kept | current)
    _bf.emit_events(candidates, overflow=summary)


if __name__ == "__main__":
    main(sys.argv[1:])
