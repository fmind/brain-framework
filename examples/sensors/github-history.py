#!/usr/bin/env python3
"""Collect one GitHub repository through gh: commits on main, or issues and PRs by modification time.

Usage: github-history.py OWNER/REPO commits|issues|pulls START END [--branch NAME]
Use window mode. Dates select commits or the latest issue/PR modification, not historical revisions.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

MAX_BYTES = 16 << 20
MAX_PAGES = 100
MAX_RECORDS = 10000
MAX_REF = 8192
MAX_LINKS = 1000
KINDS = {"commits": "commit", "issues": "issue", "pulls": "pull-request"}
TYPES = {"commits": "Commit", "issues": "Issue", "pulls": "PullRequest"}
LABELS = {"issues": "Issue", "pulls": "Pull request"}
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")


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


def repo(host: str, owner: str, name: str) -> str:
    return reference(f"repo:{host.lower()}/{quote(owner.lower(), safe='')}/{quote(name.lower(), safe='')}")


def make_record(
    identifier: str,
    title: str,
    *,
    item_type: str,
    text: str,
    time_value: str,
    url: str,
    aliases: Sequence[str],
    relations: dict[str, list[str]],
    attributes: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": identifier,
        "title": line(title, identifier),
        "@type": item_type,
        "text": text,
        "time": iso_utc(time_value),
    }
    if valid_url := reference(url):
        out["url"] = valid_url
    valid_aliases = sorted({a for a in aliases if reference(a)})
    if valid_aliases:
        out["aliases"] = valid_aliases
    cleaned = {k: list(dict.fromkeys(r for r in v if reference(r)))[:MAX_LINKS] for k, v in relations.items()}
    nonempty = {k: v for k, v in cleaned.items() if v}
    if nonempty:
        out["relations"] = nonempty
    if attributes:
        out["attributes"] = attributes
    return out


def instant(value: object) -> datetime:
    """Require an upstream timestamp with a timezone."""
    if not isinstance(value, str):
        raise InvalidError("GitHub returned an invalid timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise InvalidError("timestamps require timezones")
    return result


def string(value: object) -> str:
    """Require provider text instead of silently dropping a malformed field."""
    if not isinstance(value, str):
        raise InvalidError("GitHub returned invalid text")
    return value


def request(endpoint: str, parameters: dict[str, str]) -> tuple[Any, str]:
    """Read one bounded API reply through the explicitly selected GitHub host."""
    argv = ["gh", "api", "--hostname", "github.com", "--method", "GET", "--include", endpoint]
    for key, value in parameters.items():
        argv.extend(["--raw-field", f"{key}={value}"])
    raw = run(argv, timeout=60, max_bytes=MAX_BYTES).decode("utf-8")
    parts = re.split(r"\r?\n\r?\n", raw, maxsplit=1)
    if len(parts) != 2 or not re.match(r"HTTP/\S+ 200(?: |\r?\n)", parts[0]):
        raise InvalidError("GitHub returned an invalid response")
    headers, body = parts
    return json.loads(body), headers


def page(endpoint: str, parameters: dict[str, str], number: int) -> tuple[list[dict[str, Any]], bool]:
    """Inspect pagination headers; never execute or follow an upstream URL."""
    result, headers = request(endpoint, {**parameters, "per_page": "100", "page": str(number)})
    if not isinstance(result, list) or len(result) > 100 or any(not isinstance(item, dict) for item in result):
        raise InvalidError("GitHub returned an invalid page")
    next_page = False
    relations: set[str] = set()
    for header in headers.splitlines()[1:]:
        if header.lower().startswith("link:"):
            for link in header.partition(":")[2].split(","):
                match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*', link)
                if not match or match[2] in relations:
                    raise InvalidError("GitHub returned invalid pagination")
                relations.add(match[2])
                if match[2] == "next":
                    target = urlsplit(match[1])
                    # Requests use our own endpoint and parameters. Reject a cursor we cannot
                    # reproduce instead of silently requesting a different page and claiming success.
                    if (
                        target.scheme != "https"
                        or target.netloc != "api.github.com"
                        or target.fragment
                        or parse_qs(target.query).get("page") != [str(number + 1)]
                    ):
                        raise InvalidError("GitHub returned invalid pagination")
                    next_page = True
    if next_page and not result:
        raise InvalidError("GitHub returned an empty intermediate page")
    return result, next_page


def record(repository: str, kind: str, item: dict[str, Any]) -> dict[str, Any]:
    """Keep selected evidence and stable identities; raw API objects never reach attributes."""
    owner, name = repository.split("/", 1)
    repo_ref = repo("github.com", owner, name)
    attributes: dict[str, Any]
    if kind == "commits":
        key = string(item["sha"])
        if not re.fullmatch(r"[0-9a-f]{40}", key):
            raise InvalidError("GitHub returned an invalid commit identity")
        details = item["commit"]
        message = string(details["message"])
        when = string(details["committer"]["date"])
        author = string(details["author"]["name"])
        title = message.partition("\n")[0]
        text = f"Repository: {repository}\nCommit: {key}\nAuthor: {author}\n\n{message}"
        attributes = {"commit": key, "author": author}
        url = f"https://github.com/{repository}/commit/{key}"
        alias = f"commit:github.com/{repository}/{key}"
    else:
        number = item["number"]
        if type(number) is not int or number <= 0:
            raise InvalidError("GitHub returned an invalid issue identity")
        key = str(number)
        title = string(item["title"])
        when = string(item["created_at"])
        updated = string(item["updated_at"])
        instant(updated)
        state = item["state"]
        if state not in ("open", "closed"):
            raise InvalidError("GitHub returned an invalid issue state")
        pull = item.get("pull_request", {})
        # The listing states each PR's merge time; without it a merged PR would be saved as closed.
        if not isinstance(pull, dict) or (kind == "pulls" and "merged_at" not in pull):
            raise InvalidError("GitHub listed a pull request without its merge time")
        merged = pull.get("merged_at")
        if merged is not None:
            instant(merged)
            state = "merged"
        body = "" if item["body"] is None else string(item["body"])
        attributes = {"number": number, "status": state, "updated": iso_utc(updated)}
        if merged:
            attributes["merged"] = merged
        text = f"Repository: {repository}\n{LABELS[kind]}: #{number}\nState: {state}\n\n{body}"
        route = "pull" if kind == "pulls" else "issues"
        url = f"https://github.com/{repository}/{route}/{number}"
        alias = f"github:{repository}/{route}/{number}"
    instant(when)
    attributes["kind"] = KINDS[kind]
    return make_record(
        f"{repository}/{kind}/{key}",
        f"{repository}: {title}",
        item_type=TYPES[kind],
        text=text,
        time_value=when,
        url=url,
        aliases=[alias],
        relations={"repository": [repo_ref] if repo_ref else []},
        attributes=attributes,
    )


def collect(repository: str, kind: str, start: str, end: str, branch: str) -> list[dict[str, Any]]:
    """Finish the requested interval before returning; overflow and incomplete pages fail closed."""
    begin, finish = instant(start), instant(end)
    if begin >= finish:
        raise InvalidError("START must precede END")
    endpoint = f"repos/{repository}/" + ("commits" if kind == "commits" else "issues")
    # Provider lower bounds can be exclusive; fetch one extra second and filter locally.
    cursor = begin - timedelta(seconds=1)
    parameters = (
        {"sha": branch, "since": cursor.isoformat(), "until": end}
        if kind == "commits"
        else {"state": "all", "sort": "updated", "direction": "asc"}
    )
    records = []
    seen: dict[str, datetime] = {}
    size = 2
    number = 1
    for _ in range(MAX_PAGES):
        query = parameters if kind == "commits" else {**parameters, "since": cursor.isoformat()}
        items, more = page(endpoint, query, number)
        reached_end = False
        for item in items:
            when = instant(item["commit"]["committer"]["date"] if kind == "commits" else item["updated_at"])
            if when >= finish:
                # Issues and PRs come in modification order: the rest belongs to later windows.
                reached_end = kind != "commits"
                continue
            identity = string(item["sha"]) if kind == "commits" else str(item["number"])
            if identity in seen:
                # A restarted issue page repeats the last second read; any other repeat means a change mid-run.
                if kind == "commits" or seen[identity] != when:
                    raise InvalidError("GitHub repeated an identity; retry the window")
                continue
            seen[identity] = when
            if when < begin:
                continue
            # The repository issues endpoint lists PRs too, with their merge time.
            if kind != "commits" and ("pull_request" in item) != (kind == "pulls"):
                continue
            value = record(repository, kind, item)
            size += len(json.dumps(value, ensure_ascii=True).encode()) + 2
            if size > MAX_BYTES or len(records) >= MAX_RECORDS:
                raise InvalidError("collection exceeds its limit; narrow the window")
            records.append(value)
        if not more or reached_end:
            return records
        number += 1
        if kind != "commits":
            # Restart after the last modification read rather than at the next page number: a change during
            # the run moves an object to the end of the order, which would shift an unread object onto a page
            # already read. Only a page within two seconds cannot advance so; it continues by page number.
            following = instant(items[-1]["updated_at"]) - timedelta(seconds=1)
            if instant(items[0]["updated_at"]) < following:
                cursor, number = following, 1
    raise InvalidError("collection exceeds 100 pages; narrow the window")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("kind", choices=("commits", "issues", "pulls"))
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", args.repository):
        parser.error("repository must be OWNER/REPO on github.com")
    if not args.branch or len(args.branch) > 1024 or CONTROL.search(args.branch):
        parser.error("branch must be a bounded name without control characters")
    cli(
        lambda: emit(
            collect(args.repository.lower(), args.kind, args.start, args.end, args.branch), max_bytes=MAX_BYTES
        ),
        "GitHub collection failed",
        "invalid response or provider failure; check gh access and retry",
    )


if __name__ == "__main__":
    main()
