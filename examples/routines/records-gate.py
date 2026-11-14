#!/usr/bin/env python3
"""Records gate: succeed (exit 0) when a source gained or changed matching records in a window, else exit 1.

Usage:
  records-gate.py SOURCE [SOURCE...] [--where ATTRIBUTE=VALUE]... [--period PERIOD]
                  --end ISO --state DIR [--start ISO]

A job's `gate:` runs this script before invoking a model harness. For each SOURCE, it streams
`bf export --kind items --scope memories/SOURCE` from the first `bf` on PATH and keeps the records whose `observed`
time, when BF first collected or last changed them, falls from 10 minutes before START until END. `--where` keeps
records whose scalar attribute, as the export lists it, reads VALUE; repeated, every condition must hold.

When one or more matching records are unseen, it persists them in STATE/records-gate.json, prints `{"events": [...]}`
on stdout, and exits 0 so the gated job runs. When nothing matches, it prints `{"events": []}` and exits 1 so BF
skips the run as `gated`. Invalid arguments exit 2; export or state failures print one diagnostic on stderr quoting no
record and exit 2 without recording state.
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
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

# `observed` is stamped when a collection starts, but its records reach the cache when it ends: a collection still
# running when the previous check exported shows its records later, dated before this window. Reach back to them;
# the remembered keys stop a repeat.
SLACK = timedelta(minutes=10)
DEFAULT_LOOKBACK = timedelta(days=1)
# Reported keys are forgotten after 35 days, so a schedule whose window restarts reports nothing twice in that time.
KEEP = timedelta(days=35)
STATE = "records-gate.json"
TEXT_CHARS = 280
MAX_SOURCES = 32
MAX_EVENTS = 64
MAX_EVENT_BYTES = 65_536
MAX_KEY_CHARS = 256
# Below the gate's typical timeout, so the gate can still report why it stopped; a lower job timeout stops it first.
TIMEOUT = 100
LINE_BYTES = 262_144
STDERR_BYTES = 65_536
STATE_BYTES = 64 << 20
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def fail(message: str, code: int = 2) -> NoReturn:
    """Stop with one diagnostic that names the failure and quotes no record."""
    sys.stderr.write(f"records-gate: {message}\n")
    sys.exit(code)


def visible(value: str) -> str:
    """Escape control and invisible Unicode format characters so hidden controls stay explicit."""
    parts: list[str] = []
    for ch in value:
        cat = unicodedata.category(ch)
        if cat in {"Cc", "Cf", "Cs"}:
            code = ord(ch)
            parts.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            parts.append(ch)
    return "".join(parts)


def canonical_time(value: datetime) -> str:
    """UTC timestamp formatted with microsecond precision as a stable event key suffix."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def resolve_window(start_arg: str, end_arg: str, state_arg: str) -> tuple[datetime, datetime, Path]:
    """Validate `--end`/`--until`, `--state`, and optional `--start`/`--since` arguments."""
    if not end_arg or not state_arg:
        fail("--end and --state are required; pass --end {{end}} --state {{state}} in bf.yaml", 2)
    end = instant(end_arg)
    if end is None:
        fail("expected an ISO 8601 timestamp with timezone offset for --end", 2)
    if not start_arg:
        start = end - DEFAULT_LOOKBACK
    else:
        start = instant(start_arg)
        if start is None:
            fail("expected an ISO 8601 timestamp with timezone offset for --start", 2)
        if start >= end:
            fail("--start must precede --end", 2)
    folder = Path(state_arg)
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        fail("invalid --state directory: must not be a symlink or file", 2)
    return start, end, folder


def _kill_child(child: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, OSError):
        child.kill()
    with suppress(OSError, subprocess.SubprocessError):
        child.wait(timeout=5)


def _bf_version(bf: str) -> str:
    try:
        res = subprocess.run(  # noqa: S603 - explicit resolved binary path
            [bf, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip().splitlines()[0].strip() if res.returncode == 0 and res.stdout.strip() else ""


def export(bf: str, scope: str, deadline: float) -> Generator[dict[str, Any]]:
    """Stream each line of `bf export --kind items --scope SCOPE`, failing on any sign of incomplete evidence."""
    try:
        # Keep the child in the same process group so stopping the gate stops its export too.
        child = subprocess.Popen(  # noqa: S603 - validated executable and fixed argv
            [bf, "export", "--kind", "items", "--scope", scope],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        fail(f"could not start bf export of {scope}")
    stdout, stderr = child.stdout, child.stderr
    if stdout is None or stderr is None:
        _kill_child(child)
        fail(f"could not capture output from bf export of {scope}")
    sel = selectors.DefaultSelector()
    sel.register(stdout.fileno(), selectors.EVENT_READ, "out")
    sel.register(stderr.fileno(), selectors.EVENT_READ, "err")
    out_buf = bytearray()
    err_buf = bytearray()
    line_num = 0
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_child(child)
                fail(f"bf export of {scope} ran past the gate's {TIMEOUT}-second limit; narrow it with --period")
            for key, _ in sel.select(timeout=min(remaining, 0.5)):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                if key.data == "err":
                    if len(err_buf) < STDERR_BYTES:
                        err_buf.extend(chunk[: STDERR_BYTES - len(err_buf)])
                    continue
                out_buf.extend(chunk)
                while (nl := out_buf.find(b"\n")) != -1:
                    if nl > LINE_BYTES:
                        _kill_child(child)
                        fail(f"bf export of {scope} printed line {line_num + 1} longer than {LINE_BYTES} bytes")
                    raw_line = bytes(out_buf[:nl])
                    del out_buf[: nl + 1]
                    line_num += 1
                    if raw_line.strip():
                        yield _parse_export_line(raw_line, scope, line_num)
                if len(out_buf) > LINE_BYTES:
                    _kill_child(child)
                    fail(f"bf export of {scope} printed line {line_num + 1} longer than {LINE_BYTES} bytes")
        if out_buf.strip():
            if len(out_buf) > LINE_BYTES:
                _kill_child(child)
                fail(f"bf export of {scope} printed line {line_num + 1} longer than {LINE_BYTES} bytes")
            line_num += 1
            yield _parse_export_line(bytes(out_buf), scope, line_num)
        code = child.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _kill_child(child)
        fail(f"bf export of {scope} ran past the gate's {TIMEOUT}-second limit; narrow it with --period")
    finally:
        sel.close()
        for pipe in (stdout, stderr):
            with suppress(OSError):
                pipe.close()

    diag = err_buf.decode("utf-8", errors="replace")
    if code == 2 and "--scope" in diag:
        version = _bf_version(bf)
        if version:
            fail(f"bf {version} cannot export a scope; install bf 21.3.0 or later")
    if code != 0:
        fail(f"bf export of {scope} exited with status {code}; run bf export --kind items --scope {scope} to see why")
    if diag.strip():
        fail(
            f"bf export of {scope} reported incomplete evidence; run bf export --kind items --scope {scope} to see why"
        )


def _parse_export_line(raw_line: bytes, scope: str, line_num: int) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_line)
    except (UnicodeError, json.JSONDecodeError):
        fail(f"bf export of {scope} printed an invalid line {line_num}")
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("brain"), str)
        or not parsed["brain"]
        or not isinstance(parsed.get("ref"), str)
        or not parsed["ref"]
    ):
        fail(f"bf export of {scope} printed an invalid line {line_num}")
    return parsed


def address(brain: str, ref: str) -> str:
    """A record's address, spelled as a search result's `uri`."""
    return f"bf://{brain}/{quote(ref, safe='/@:')}"


def spelled(value: object) -> str | None:
    """An attribute value as `--where` spells it: text as is, true or false, a number as JSON writes it."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    return None


def matches(record: dict[str, Any], where: list[tuple[str, str]]) -> bool:
    """Whether every condition holds against the record's exported `attributes`. Lists or objects never match."""
    attributes = record.get("attributes", {})
    return isinstance(attributes, dict) and all(spelled(attributes.get(field)) == expected for field, expected in where)


def line(title: object) -> str:
    """A record title as event text: one line of at most TEXT_CHARS characters, hidden characters made visible."""
    text = " ".join(str(title).split()) or "untitled"
    return visible(text if len(text) <= TEXT_CHARS else text[: TEXT_CHARS - 1] + "…")


def _short_key(key: str, group: str) -> str:
    if len(key) <= MAX_KEY_CHARS:
        return key
    prefix = group or "event"
    return f"{prefix}:sha256:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _bounded_events(
    candidates: list[dict[str, Any]], overflow: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]]
) -> list[dict[str, Any]]:
    normalized = [
        {
            "key": _short_key(str(item["key"]), str(item.get("group", ""))),
            "text": str(item["text"]),
            **({"ref": str(item["ref"])} if item.get("ref") else {}),
        }
        for item in candidates
    ]

    def fits(items: list[dict[str, Any]]) -> bool:
        return (
            len(items) <= MAX_EVENTS
            and len(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_EVENT_BYTES
        )

    if fits(normalized):
        return normalized
    lo, hi = 0, min(len(normalized), MAX_EVENTS - 1)
    best: list[dict[str, Any]] = []
    while lo <= hi:
        mid = (lo + hi) // 2
        tail = dict(overflow(candidates[mid:]))
        summary = {
            "key": _short_key(str(tail["key"]), ""),
            "text": str(tail["text"]),
            **({"ref": str(tail["ref"])} if tail.get("ref") else {}),
        }
        attempt = [*normalized[:mid], summary]
        if fits(attempt):
            best = attempt
            lo = mid + 1
        else:
            hi = mid - 1
    if not best:
        tail = dict(overflow(candidates))
        best = [
            {
                "key": _short_key(str(tail["key"]), ""),
                "text": str(tail["text"])[:TEXT_CHARS],
                **({"ref": str(tail["ref"])} if tail.get("ref") else {}),
            }
        ]
    return best


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
    if path.is_symlink():
        fail(f"cannot read {STATE} in state directory: symlinks are not allowed")
    if not path.exists():
        return set()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        fail(f"cannot read {STATE} in state directory; delete it to report this window's records again")
    try:
        data = os.read(fd, STATE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > STATE_BYTES:
        fail(f"cannot read {STATE} in state directory; delete it to report this window's records again")
    try:
        state = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        state = None
    seen = state.get("seen") if isinstance(state, dict) else None
    if not isinstance(seen, list) or not all(isinstance(key, str) for key in seen):
        fail(f"cannot read {STATE} in state directory; delete it to report this window's records again")
    return set(seen)


def save(path: Path, seen: set[str]) -> None:
    """Replace the state file whole, refusing symlinks so an interrupted write keeps the previous file."""
    parent = path.parent
    if parent.is_symlink() or path.is_symlink():
        fail(f"cannot save {STATE}: state path must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True)
    payload = encoded(bounded(seen))
    tmp = parent / f".{STATE}.{os.getpid()}.tmp"
    with suppress(FileNotFoundError):
        if tmp.is_symlink():
            fail(f"cannot save {STATE}: unexpected symlink at temp path")
        tmp.unlink()
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        fail(f"cannot save {STATE} in state directory")
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(path)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="+", type=source, metavar="SOURCE", help="a source name, such as mail")
    parser.add_argument("--where", action="append", default=[], type=condition, metavar="FIELD=VALUE")
    parser.add_argument("--period", type=period, help="export only this period of each source, such as 7d")
    parser.add_argument("--start", "--since", dest="start", default="", metavar="ISO", help="window start timestamp")
    parser.add_argument("--end", "--until", dest="end", required=True, metavar="ISO", help="window end timestamp")
    parser.add_argument("--state", required=True, metavar="DIR", help="private directory for remembered keys")
    args = parser.parse_args(argv)
    sources = list(dict.fromkeys(args.sources))
    if len(sources) > MAX_SOURCES:
        parser.error(f"at most {MAX_SOURCES} sources")
    start, end, folder = resolve_window(args.start, args.end, args.state)
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
                    stamp, uri = canonical_time(observed), address(record["brain"], record["ref"])
                    if f"{uri}@{stamp}" not in seen:
                        found.append((stamp, uri, name, line(record.get("title", ""))))
    # The newest changes first: the overflow event counts the older ones.
    found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    candidates: list[dict[str, Any]] = [
        {"key": f"{uri}@{stamp}", "text": text, "ref": uri, "group": name} for stamp, uri, name, text in found
    ]

    def summary(dropped: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
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
    # Save state before reporting so an unsuccessful state write never emits duplicate triggers.
    if current or kept != seen:
        save(folder / STATE, kept | current)
    if not candidates:
        sys.stdout.write('{"events": []}\n')
        return 1
    reply = {"events": _bounded_events(candidates, overflow=summary)}
    sys.stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
