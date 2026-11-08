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
import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Generator
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

# BF names brains, sources and fields this way.
NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
# `observed` is stamped when a collection starts, but its records reach the cache when it ends: a collection still
# running when the previous check exported shows its records later, dated before this window. Reach back to them;
# the remembered keys stop a repeat.
SLACK = timedelta(minutes=10)
# Reported keys are forgotten after 35 days, so a schedule whose window restarts reports nothing twice in that time.
KEEP = timedelta(days=35)
STATE = "records-gate.json"
# A gate prints at most 64 events within 65,536 encoded bytes: keep one event and some bytes for the overflow event.
EVENTS = 63
EVENT_BYTES = 60_000
KEY_CHARS = 256
REF_CHARS = 4096
TEXT_CHARS = 280
MAX_SOURCES = 32
# Below the gate's default 120-second timeout, so the gate can still report why it stopped; a lower gate `timeout` stops
# the gate and its export first. A whole-source export reads every record of the source: see the README for a measured
# time.
TIMEOUT = 100
# A record file holds up to 16 MiB, and escaping can double its text in the export: one line never holds more.
LINE_BYTES = 64 << 20
STATE_BYTES = 64 << 20
ERROR_BYTES = 64 << 10
# `bf export --scope` first shipped in bf 21.3.0.
NEEDED = (21, 3)


def fail(message: str, code: int = 1) -> NoReturn:
    """Stop with one diagnostic that names the failure and quotes no record."""
    sys.stderr.write(f"records-gate: {message}\n")
    raise SystemExit(code)


def instant(value: object) -> datetime | None:
    """An aware timestamp, or None when the value is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def canonical(moment: datetime) -> str:
    """One spelling per instant, whatever offset the export used, so a remembered key matches across time zones."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def condition(value: str) -> tuple[str, str]:
    """A `--where FIELD=VALUE` pair: a BF field name, then any value."""
    field, separator, expected = value.partition("=")
    if not separator or not NAME.fullmatch(field):
        raise argparse.ArgumentTypeError("expected FIELD=VALUE with a field name such as kind")
    return field, expected


def source(value: str) -> str:
    if not NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a source name such as mail")
    return value


def period(value: str) -> str:
    if not value or "/" in value or value != value.strip():
        raise argparse.ArgumentTypeError("expected one period such as 7d or 2026-09")
    return value


def window() -> tuple[datetime, datetime, Path]:
    """BF_START, BF_END and BF_STATE_DIR, which BF sets for a gate; run by hand, set them yourself."""
    start, end = instant(os.environ.get("BF_START")), instant(os.environ.get("BF_END"))
    folder = os.environ.get("BF_STATE_DIR", "")
    if start is None or end is None or not folder:
        fail("BF_START, BF_END and BF_STATE_DIR must be set; BF sets them when it runs a gate", 2)
    return start, end, Path(folder)


def version(bf: str) -> tuple[int, ...] | None:
    """The installed bf's version, or None when it cannot tell."""
    try:
        result = subprocess.run(  # noqa: S603 - literal argv
            [bf, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.fullmatch(rb"(\d+)\.(\d+)\.(\d+)\s*", result.stdout[:64])
    return tuple(int(part) for part in found.groups()) if found and not result.returncode else None


def stop(child: subprocess.Popen[bytes]) -> None:
    """End bf, reap it and close its pipes."""
    if child.poll() is None:
        with suppress(OSError):
            child.kill()
    with suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5)
    for pipe in (child.stdout, child.stderr):
        if pipe is not None:
            pipe.close()


def export(bf: str, scope: str, deadline: float) -> Generator[dict]:
    """Stream each line of `bf export --kind items --scope SCOPE`, failing on any sign of incomplete evidence.

    The export is never held whole: a large source prints tens of megabytes. Only one line is, within LINE_BYTES.
    """
    try:
        # bf reads BF_BRAIN, the brain running the gate. It stays in the gate's process group, where BF's stop of the
        # gate, on a timeout or a cancellation, reaches it too; the export starts no program of its own.
        child = subprocess.Popen(  # noqa: S603 - literal argv: no shell
            [bf, "export", "--kind", "items", "--scope", scope],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        fail("could not start bf; check that it is installed and executable")
    pending, errors, number = bytearray(), bytearray(), 0
    try:
        if child.stdout is None or child.stderr is None:
            fail("could not read bf's output")
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, "out")
            selector.register(child.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail(f"bf export of {scope} ran past the gate's {TIMEOUT}-second limit; narrow it with --period")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 1 << 16)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "err":
                        # bf's diagnostics are content-free, but the gate reports only that they exist.
                        errors += chunk[: max(0, ERROR_BYTES - len(errors))]
                    else:
                        begin = 0
                        while (newline := chunk.find(b"\n", begin)) >= 0:
                            pending += chunk[begin:newline]
                            begin = newline + 1
                            number += 1
                            yield item(bytes(pending), scope, number)
                            pending.clear()
                        pending += chunk[begin:]
                        if len(pending) > LINE_BYTES:
                            fail(f"bf export of {scope} printed line {number + 1} longer than {LINE_BYTES} bytes")
        try:
            code = child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            fail(f"bf export of {scope} ran past the gate's {TIMEOUT}-second limit; narrow it with --period")
        if pending:
            yield item(bytes(pending), scope, number + 1)
    finally:
        stop(child)
    if code == 2 and (found := version(bf)) is not None and found[:2] < NEEDED:
        fail(f"bf {'.'.join(map(str, found))} cannot export a scope; install bf 21.3.0 or later")
    if code:
        fail(f"bf export of {scope} exited with status {code}; run bf export --kind items --scope {scope} to see why")
    # bf notes a busy cache, or a file that changed or became unreadable, on stderr: what it printed may miss records.
    if any(line.startswith(b"bf:") for line in errors.splitlines()) or len(errors) >= ERROR_BYTES:
        fail(
            f"bf export of {scope} reported incomplete evidence; run bf export --kind items --scope {scope} to see why"
        )


def item(line: bytes, scope: str, number: int) -> dict:
    """One exported item; anything but a JSON object naming its brain and ref stops the gate."""
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        value = None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("brain"), str)
        or not NAME.fullmatch(value["brain"])
        or not isinstance(value.get("ref"), str)
        or not value["ref"]
    ):
        fail(f"bf export of {scope} printed an invalid line {number}")
    return value


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


def matches(record: dict, where: list[tuple[str, str]]) -> bool:
    """Whether every condition holds. The export lists declared single-value fields of up to 200 characters only: a
    `cardinality: many` field never matches."""
    fields = record.get("fields", {})
    return isinstance(fields, dict) and all(spelled(fields.get(field)) == expected for field, expected in where)


def visible(text: str) -> str:
    """Escape controls, line and paragraph separators and invisible format characters, which bf replies escape and
    JSON decoding restored."""
    return "".join(
        char.encode("unicode_escape").decode() if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in text
    )


def line(title: object) -> str:
    """A record title as event text: one line of at most TEXT_CHARS characters, hidden characters made visible."""
    text = " ".join(str(title).split()) or "untitled"
    return visible(text if len(text) <= TEXT_CHARS else text[: TEXT_CHARS - 1] + "…")


def event(name: str, uri: str, observed: str, text: str) -> dict:
    """One event: a key unique to this version of the record, its title and its address for bf read."""
    key = f"{uri}@{observed}"
    if len(key) > KEY_CHARS:
        key = f"{name}:sha256:{hashlib.sha256(key.encode()).hexdigest()}"
    return {"key": key, "text": text, **({"ref": uri} if len(uri) <= REF_CHARS else {})}


def size(value: dict) -> int:
    """Bytes the event takes in BF's encoding of the event list, with its separating comma."""
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) + 1


def load(path: Path) -> set[str]:
    """The keys already reported; a missing file reports everything in the window, a damaged one stops the gate."""
    try:
        with path.open("rb") as stream:
            data = stream.read(STATE_BYTES + 1)
    except FileNotFoundError:
        return set()
    except OSError:
        data = b""
    try:
        seen = json.loads(data)["seen"] if len(data) <= STATE_BYTES else None
    except (ValueError, KeyError, TypeError):
        seen = None
    if not isinstance(seen, list) or not all(isinstance(key, str) for key in seen):
        fail(f"cannot read {STATE} in BF_STATE_DIR; delete it to report this window's records again")
    return set(seen)


def encoded(seen: set[str]) -> bytes:
    return json.dumps({"seen": sorted(seen)}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def bounded(seen: set[str]) -> set[str]:
    """The keys the state file can hold within STATE_BYTES, newest first, so load() can read it back.

    The oldest are dropped: a later check covers only newer records, so it cannot report them again.
    """
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


def save(path: Path, seen: set[str]) -> None:
    """Replace the state file whole, so an interrupted write keeps the previous one."""
    data = encoded(bounded(seen))
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{STATE}.", suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
        fail(f"cannot write {STATE} in BF_STATE_DIR; check that the folder exists and is writable")


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
                    stamp, uri = canonical(observed), address(record["brain"], record["ref"])
                    if f"{uri}@{stamp}" not in seen:
                        found.append((stamp, uri, name, line(record.get("title", ""))))
    # The newest changes first: the overflow event counts the older ones.
    found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    events: list[dict] = []
    left: Counter[str] = Counter()
    used = 2
    for stamp, uri, name, text in found:
        candidate = event(name, uri, stamp, text)
        if len(events) < EVENTS and used + size(candidate) <= EVENT_BYTES:
            events.append(candidate)
            used += size(candidate)
        else:
            left[name] += 1
    if left:
        counts = ", ".join(f"{name} {count}" for name, count in sorted(left.items()))
        single = next(iter(left)) if len(left) == 1 else ""
        scope = f"memories/{single}/{args.period}" if single and args.period else f"memories/{single}"
        events.append(
            {
                "key": f"{single}:more" if single else "records:more",
                "text": f"{left.total()} more new or changed records are not listed: {counts}. Read their source pages.",
                **({"ref": scope} if single else {}),
            }
        )
    current = {f"{uri}@{stamp}" for stamp, uri, *_ in found}
    horizon = end - KEEP
    kept = {key for key in seen if (moment := instant(key.rpartition("@")[2])) is not None and moment >= horizon}
    # Saved before printing: a gate that printed but could not remember would report the same records again.
    if current or kept != seen:
        save(folder / STATE, kept | current)
    sys.stdout.write(json.dumps({"events": events}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
