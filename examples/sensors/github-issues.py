#!/usr/bin/env python3
"""Collect one GitHub repository's issues updated in the window through the owner's gh CLI.

Usage: github-issues.py OWNER/REPO START END
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

MAX_ISSUES = 500
MAX_BYTES = 16 << 20
MAX_REF = 8192
TIMEOUT = 60
FIELDS = "number,title,body,state,stateReason,author,assignees,labels,createdAt,updatedAt,closedAt,url"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")
_PEM_TAG = r"[A-Z0-9 ]{0,32}PRIVATE KEY-----"
_PEM_BODY = rf"(?:(?!-----(?:BEGIN|END) {_PEM_TAG})[\s\S]){{0,16384}}"
_KV_KEY = r"(?i:\b(?:password|passwd|(?:client[_-]?)?secret|api[_-]?key|(?:access|refresh)[_-]?token))"
_KV_VAL = r'(?:\\?"[^"\r\n]{1,512}"|\\?\'[^\'\r\n]{1,512}\'|[^\s"\',;}{\]]{1,512})'
_SECRET_PATTERNS = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ASIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|ya29\.[A-Za-z0-9._-]{8,}"
    r"|xox[baprs]-[0-9A-Za-z-]+"
    r"|sk-[A-Za-z0-9_-]{20,}"
    rf"|-----BEGIN {_PEM_TAG}{_PEM_BODY}(?:-----END {_PEM_TAG})?"
    r"|(?<=://)[^/\s:@]{1,64}:[^/\s@]{1,128}(?=@)"
    rf"|{_KV_KEY}(?:\\?[\"'])?\s*[:=]\s*{_KV_VAL}"
    r"|(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}))"
)


class InvalidError(ValueError):
    """Provider output or inputs violated the expected contract."""


class ProviderError(Exception):
    """Provider subprocess failed, timed out, or could not start."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def run(argv: Sequence[str], *, timeout: float, max_bytes: int) -> bytes:
    """Execute one provider CLI from / with bounded output and process-group reaping."""
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ProviderError(f"cannot start {argv[0]}") from error
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        raise RuntimeError("subprocess pipes missing")
    out_fd, err_fd = stdout.fileno(), stderr.fileno()
    chunks: list[bytes] = []
    overflow = False

    def drain_out() -> None:
        nonlocal overflow
        total = 0
        while data := os.read(out_fd, 65536):
            total += len(data)
            if total > max_bytes:
                overflow = True
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                return
            chunks.append(data)

    def drain_err() -> None:
        while os.read(err_fd, 65536):
            pass

    t_out = threading.Thread(target=drain_out, daemon=True)
    t_err = threading.Thread(target=drain_err, daemon=True)
    t_out.start()
    t_err.start()
    deadline = time.monotonic() + timeout
    try:
        while t_out.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout")
            t_out.join(timeout=min(remaining, 0.05))
        try:
            remaining = max(0.0, deadline - time.monotonic())
            code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout") from None
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        stdout.close()
        stderr.close()
    if overflow:
        raise InvalidError(f"{argv[0]} output exceeds {max_bytes} bytes")
    if code != 0:
        raise ProviderError(f"{argv[0]} exited with status {code}", status=code)
    return b"".join(chunks)


def line(value: str, fallback: str = "") -> str:
    cleaned = " ".join(CONTROL.sub(" ", value).split())
    return (cleaned[:4096] or fallback)[:4096] if (cleaned or fallback) else ""


def reference(value: str) -> str:
    if (
        not value
        or len(value) > MAX_REF
        or any(c.isspace() for c in value)
        or CONTROL.search(value)
        or not IDENTITY.fullmatch(value)
    ):
        return ""
    return value


def iso_utc(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise InvalidError("timestamps require timezones")
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def redact(text: str) -> str:
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


def emit(records: list[dict[str, Any]], *, max_bytes: int = MAX_BYTES) -> None:
    payload = (json.dumps(records, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > max_bytes:
        raise InvalidError(f"output exceeds {max_bytes} bytes")
    sys.stdout.buffer.write(payload)


def cli(action: Callable[[], None], summary: str, hint: str) -> None:
    try:
        action()
    except (InvalidError, ProviderError) as error:
        sys.stderr.write(f"{summary}: {error}.\n")
        sys.exit(1)
    except (OSError, UnicodeError, ValueError, LookupError, ArithmeticError, RecursionError):
        sys.stderr.write(f"{summary}; {hint}.\n")
        sys.exit(1)


def login(value: object) -> str:
    """A GitHub account's login, or "" for a deleted (ghost) account."""
    name = value.get("login") if isinstance(value, dict) else None
    return name if isinstance(name, str) else ""


def make_record(
    identifier: str,
    title: str,
    *,
    item_type: str,
    text: str,
    time_value: str,
    url: str,
    attributes: dict[str, object],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": identifier,
        "title": line(title, identifier),
        "type": item_type.lower(),
        "text": text,
        "time": iso_utc(time_value),
    }
    if isinstance(attributes.get("status"), str) and attributes["status"]:
        out["status"] = attributes["status"]
    if isinstance(attributes.get("updated"), str) and attributes["updated"]:
        out["updated"] = iso_utc(str(attributes["updated"]))
    if valid_url := reference(url):
        out["url"] = valid_url
    if attributes:
        out["attributes"] = attributes
    return out


def issue(repository: str, item: object, start: datetime, end: datetime) -> dict[str, Any] | None:
    """One issue as a record; None when its last update falls outside the window the search widened."""
    if not isinstance(item, dict):
        raise InvalidError("gh returned an invalid issue")
    number, title, state = item.get("number"), item.get("title"), item.get("state")
    created, updated = item.get("createdAt"), item.get("updatedAt")
    assignees, labels = item.get("assignees") or [], item.get("labels") or []
    if (
        type(number) is not int
        or number <= 0
        or not isinstance(title, str)
        or state not in ("OPEN", "CLOSED")
        or not isinstance(created, str)
        or not isinstance(updated, str)
        or not isinstance(assignees, list)
        or not isinstance(labels, list)
    ):
        raise InvalidError("gh returned an invalid issue")
    updated_at = datetime.fromisoformat(updated)
    if updated_at.tzinfo is None:
        raise InvalidError("timestamps require timezones")
    if not start <= updated_at < end:
        return None
    author = login(item.get("author"))
    people = [login(person_obj) for person_obj in assignees]
    names = sorted(
        {label["name"] for label in labels if isinstance(label, dict) and isinstance(label.get("name"), str)}
    )
    status = state.lower()
    reason = item.get("stateReason")
    body = item.get("body")
    lines = [f"Repository: {repository}", f"Issue: #{number}", f"Status: {status}"]
    lines += [f"Author: {author}"] if author else []
    lines += [f"Assignees: {', '.join(filter(None, people))}"] if any(people) else []
    lines += [f"Labels: {', '.join(names)}"] if names else []
    return make_record(
        f"{repository}/issues/{number}",
        f"{repository}: {title}",
        item_type="Issue",
        # Issue bodies sometimes quote a pasted token: mask common credential shapes before saving.
        text="\n".join(lines) + ("\n\n" + redact(body) if isinstance(body, str) and body else ""),
        time_value=created,
        url=item.get("url") or "",
        attributes={
            "number": number,
            "kind": "issue",
            "status": status,
            **({"reason": reason.lower()} if isinstance(reason, str) and reason else {}),
            "labels": names,
            "updated": iso_utc(updated),
        },
    )


def collect(repository: str, start_arg: str, end_arg: str) -> list[dict[str, Any]]:
    if not REPOSITORY.fullmatch(repository):
        raise InvalidError("repository must be OWNER/REPO")
    repo_name = repository.lower()  # gh accepts any case; keep repository ids lowercase
    start, end = datetime.fromisoformat(start_arg), datetime.fromisoformat(end_arg)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise InvalidError("START and END need timezones, with START before END")
    # GitHub search ranges are inclusive and take whole seconds: widen outward, then filter exactly.
    first = datetime.fromtimestamp(math.floor(start.timestamp()), UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    last = datetime.fromtimestamp(math.ceil(end.timestamp()), UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    argv = ["gh", "issue", "list", "--repo", repo_name, "--state", "all", "--search", f"updated:{first}..{last}"]
    # One more than the bound: a full answer means GitHub has more, and a partial catalog must not pass as complete.
    argv += ["--limit", str(MAX_ISSUES + 1), "--json", FIELDS]
    items = json.loads(run(argv, timeout=TIMEOUT, max_bytes=MAX_BYTES))
    if not isinstance(items, list):
        raise InvalidError("gh returned an unexpected issue list")
    if len(items) > MAX_ISSUES:
        raise InvalidError(f"the window has more than {MAX_ISSUES} updated issues; narrow it")
    found = (issue(repo_name, item, start, end) for item in items)
    return [rec for rec in found if rec is not None]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repository", help="OWNER/REPO")
    parser.add_argument("start", help="window start timestamp with timezone")
    parser.add_argument("end", help="window end timestamp with timezone")
    args = parser.parse_args()
    cli(
        lambda: emit(collect(args.repository, args.start, args.end), max_bytes=MAX_BYTES),
        "GitHub issue collection failed",
        "check gh authentication and the repository",
    )


if __name__ == "__main__":
    main()
