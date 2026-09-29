#!/usr/bin/env python3
"""Collect one GitHub repository through gh: commits on main, or issues and PRs by modification time.

Usage: github-history.py OWNER/REPO commits|issues|pulls START END [--branch NAME]
Use window mode. Dates select commits or the latest issue/PR modification, not historical revisions.
"""

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 16 << 20
MAX_PAGES = 100
MAX_RECORDS = 10000
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class InvalidError(ValueError):
    """A content-free diagnostic safe to show on stderr."""


def run(argv: list[str], limit: int, timeout: int) -> bytes:
    """Bound provider output while it runs; inherit Brain Framework's cancellable process group."""
    # Only literal provider commands reach this helper; no shell interprets argv.
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        if child.stdout is None:
            raise ValueError("provider pipe is missing")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider exceeded its timeout")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > limit:
                        raise InvalidError("a GitHub page exceeds its byte limit")
        if code := child.wait():
            raise subprocess.CalledProcessError(code, argv)
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


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
    raw = run(argv, MAX_BYTES, 60).decode("utf-8")
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
    repo_ref = "repo:github.com/" + repository
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
        merged = item.get("pull_request", {}).get("merged_at")
        if merged is not None:
            instant(merged)
            state = "merged"
        body = "" if item["body"] is None else string(item["body"])
        attributes = {"number": number, "state": state, "updated": updated}
        if merged:
            attributes["merged"] = merged
        text = f"Repository: {repository}\n{kind.title()}: #{number}\nState: {state}\n\n{body}"
        route = "pull" if kind == "pulls" else "issues"
        url = f"https://github.com/{repository}/{route}/{number}"
        alias = f"github:{repository}/{route}/{number}"
    instant(when)
    attributes["repository_refs"] = [repo_ref]
    attributes["kind"] = kind
    return {
        "id": f"{repository}/{kind}/{key}",
        "title": " ".join(CONTROL.sub(" ", f"{repository}: {title}").split())[:4096],
        "text": text,
        "time": when,
        "url": url,
        "links": [repo_ref],
        "aliases": [alias],
        "attributes": attributes,
    }


def collect(repository: str, kind: str, start: str, end: str, branch: str) -> list[dict[str, Any]]:
    """Finish the requested interval before returning; overflow and incomplete pages fail closed."""
    begin, finish = instant(start), instant(end)
    if begin >= finish:
        raise InvalidError("START must precede END")
    endpoint = f"repos/{repository}/" + ("commits" if kind == "commits" else "issues")
    # Provider lower bounds can be exclusive; fetch one extra second and filter locally.
    since = (begin - timedelta(seconds=1)).isoformat()
    parameters = (
        {"sha": branch, "since": since, "until": end}
        if kind == "commits"
        else {"state": "all", "sort": "updated", "direction": "asc", "since": since}
    )
    records = []
    seen: set[str] = set()
    size = 2
    for number in range(1, MAX_PAGES + 1):
        items, more = page(endpoint, parameters, number)
        reached_end = False
        for item in items:
            when = instant(item["commit"]["committer"]["date"] if kind == "commits" else item["updated_at"])
            identity = string(item["sha"]) if kind == "commits" else str(item["number"])
            if identity in seen:
                raise InvalidError("GitHub repeated an identity; retry the window")
            seen.add(identity)
            if when >= finish:
                reached_end = kind != "commits"
                continue
            if when < begin:
                continue
            # The repository issues endpoint discovers updated PRs; detail supplies the merge outcome.
            if kind != "commits" and ("pull_request" in item) != (kind == "pulls"):
                continue
            if kind == "pulls":
                if type(item["number"]) is not int or item["number"] <= 0:
                    raise InvalidError("GitHub returned an invalid pull request identity")
                # One extra request per selected PR: only the detail states whether it was merged.
                details, _ = request(f"repos/{repository}/pulls/{item['number']}", {})
                if not isinstance(details, dict) or details.get("number") != item["number"]:
                    raise InvalidError("GitHub returned an invalid pull request detail")
                modified = instant(details["updated_at"])
                if modified < when:
                    raise InvalidError("GitHub returned pull request detail older than its listing; retry the window")
                if modified >= finish:
                    # Modified again after the listing: its latest modification belongs to the next window.
                    continue
                item = {**details, "pull_request": {"merged_at": details["merged_at"]}}
            value = record(repository, kind, item)
            size += len(json.dumps(value, ensure_ascii=True).encode()) + 2
            if size > MAX_BYTES or len(records) >= MAX_RECORDS:
                raise InvalidError("collection exceeds its limit; narrow the window")
            records.append(value)
        if not more or reached_end:
            return records
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
    try:
        records = collect(args.repository.lower(), args.kind, args.start, args.end, args.branch)
        print(json.dumps(records, ensure_ascii=True))
    except InvalidError as error:
        print(f"GitHub collection failed: {error}", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        print(
            "GitHub collection failed: invalid response or provider failure; check gh access and retry", file=sys.stderr
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
