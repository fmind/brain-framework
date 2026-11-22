#!/usr/bin/env python3
"""Collect local coding-agent session and prompt telemetry JSON/JSONL logs into Brain Framework records."""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_ENTRIES = 5000
MAX_DEPTH = 16
FILE_BYTES = 16 << 20
TOTAL_BYTES = 64 << 20
MAX_BYTES = 16 << 20
TEXT_CHARS = 4096
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_PEM_TAG = r"[A-Z0-9 ]{0,32}PRIVATE KEY-----"
_PEM_BODY = rf"(?:(?!-----(?:BEGIN|END) {_PEM_TAG})[\s\S]){{0,16384}}"
_KV_KEY = (
    r"(?i:(?<![A-Za-z0-9])(?:password|passwd|(?:client[_-]?)?secret(?:[_-]?access[_-]?key|[_-]?key)?"
    r"|private[_-]?key|api[_-]?key"
    r"|(?:access|refresh|auth|api|session|id|bot|private|gh|github|oauth|bearer)[_-]?token))"
)
_KV_VAL = r'(?:\\?"[^"\r\n]{1,8192}"?|\\?\'[^\'\r\n]{1,8192}\'?|[^\s"\',;}{\]]{1,8192})'
REDACT = re.compile(
    r"(?:(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|\bauthorization\s*[:=]\s*basic\s+[A-Za-z0-9+/=-]{8,})"
    r"|(?<=://)[^/\s:@]{1,64}:[^/\s@]{1,128}(?=@)"
    r"|(?:gh[pousr]_|github_pat_|AKIA|ASIA|AIza|ya29\.|sk-|xox[baprs]-)[A-Za-z0-9._-]{8,}"
    rf"|-----BEGIN {_PEM_TAG}{_PEM_BODY}(?:-----END {_PEM_TAG})?|{_KV_KEY}(?:\\?[\"'])?\s*[:=]\s*{_KV_VAL})"
)


class InvalidError(ValueError):
    """Telemetry root, timestamp window, or log structure violated safety bounds."""


def parse_window_iso(value: str, label: str) -> datetime:
    raw = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as error:
        raise InvalidError(f"{label} must be an ISO 8601 timestamp with timezone") from error
    if dt.tzinfo is None:
        raise InvalidError(f"{label} must include a timezone offset")
    return dt.astimezone(UTC)


def sanitize(value: str, limit: int = TEXT_CHARS) -> str:
    cleaned = CONTROL.sub(" ", REDACT.sub("[REDACTED]", value))
    return " ".join(cleaned.split())[:limit]


def extract_prompt(obj: dict[str, Any]) -> tuple[str, str, str]:
    """Return `(role, prompt_text, timestamp_iso)` when an entry represents a user prompt or session summary."""
    role = str(obj.get("role") or obj.get("type") or "")
    msg = obj.get("message")
    if isinstance(msg, dict):
        role = str(msg.get("role") or role)
        content = msg.get("content")
    else:
        content = obj.get("prompt") or obj.get("content") or obj.get("summary") or obj.get("title") or ""
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
            if not isinstance(item, dict) or item.get("type") in {None, "text", "input_text"}
        ]
        text = " ".join(p for p in parts if p)
    elif isinstance(content, str):
        text = content
    else:
        text = ""
    ts = str(obj.get("timestamp") or obj.get("time") or obj.get("created_at") or obj.get("updated_at") or "")
    return role.lower(), sanitize(text), ts


def open_nofollow_dir(root: Path) -> int:
    resolved_parts = root.expanduser().absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(resolved_parts.anchor, flags)
    try:
        for part in resolved_parts.parts[1:]:
            if part == "..":
                raise InvalidError("ROOT must be a normalized path")
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise InvalidError("ROOT or a parent directory is a symlink or not a directory") from None
                raise
            os.close(fd)
            fd = nxt
        return fd
    except Exception:
        os.close(fd)
        raise


def collect(harness: str, root: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", harness):
        raise InvalidError("HARNESS must start with a lowercase letter and use [a-z0-9-]")
    if start >= end:
        raise InvalidError("START must be before END")

    records: list[dict[str, Any]] = []
    visited = total_bytes = 0
    root_fd = open_nofollow_dir(root)

    def walk(dir_fd: int, prefix: str = "") -> None:
        nonlocal visited, total_bytes
        if prefix.count("/") > MAX_DEPTH:
            raise InvalidError(f"{prefix}: directory tree exceeds {MAX_DEPTH} levels")
        with os.scandir(dir_fd) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            visited += 1
            if visited > MAX_ENTRIES:
                raise InvalidError(f"root exceeds {MAX_ENTRIES} entries")
            if entry.name.startswith("."):
                continue
            rel = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise InvalidError(f"{rel}: symlinks are refused")
            if stat.S_ISDIR(info.st_mode):
                sub_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
                try:
                    walk(sub_fd, rel + "/")
                finally:
                    os.close(sub_fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            suffix = Path(entry.name).suffix.lower()
            if suffix not in {".json", ".jsonl"}:
                continue
            mtime = datetime.fromtimestamp(info.st_mtime, UTC)
            if mtime < start:
                continue
            file_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
            with os.fdopen(file_fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InvalidError(f"{rel}: not a regular file")
                raw = stream.read(FILE_BYTES + 1)
            if len(raw) > FILE_BYTES:
                raise InvalidError(f"{rel}: file exceeds {FILE_BYTES} bytes")
            total_bytes += len(raw)
            if total_bytes > TOTAL_BYTES:
                raise InvalidError("telemetry input exceeds 64 MiB")
            text = raw.decode("utf-8")
            prompts: list[str] = []
            event_dts: list[datetime] = []
            items: list[dict[str, Any]] = []
            if suffix == ".json":
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    msgs = parsed.get("messages") or parsed.get("turns") or [parsed]
                    items = [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else [parsed]
                elif isinstance(parsed, list):
                    items = [m for m in parsed if isinstance(m, dict)]
            else:
                for raw_ln in raw.splitlines():
                    if stripped := raw_ln.decode("utf-8").strip():
                        parsed_line = json.loads(stripped)
                        if isinstance(parsed_line, dict):
                            items.append(parsed_line)

            for item in items:
                role, prompt_txt, ts_raw = extract_prompt(item)
                if ts_raw:
                    with contextlib.suppress(InvalidError):
                        event_dts.append(parse_window_iso(ts_raw, rel))
                if prompt_txt and role in {"", "user", "human", "prompt", "session"}:
                    prompts.append(prompt_txt)

            if not prompts:
                continue
            if event_dts:
                window_dts = [dt for dt in event_dts if start <= dt < end]
                if not window_dts:
                    continue
                latest_dt = max(window_dts)
            elif start <= mtime < end:
                latest_dt = mtime
            else:
                continue
            rec_hash = hashlib.sha256(f"{harness}:{rel}".encode()).hexdigest()[:16]
            when_iso = latest_dt.isoformat().replace("+00:00", "Z")
            joined = "\n".join(prompts[:20])[:TEXT_CHARS]
            records.append(
                {
                    "id": f"{harness}-{rec_hash}",
                    "type": "agent-session",
                    "title": f"{harness}: {prompts[0][:80]}",
                    "text": joined,
                    "time": when_iso,
                    "updated": when_iso,
                    "tags": ["telemetry", harness],
                    "attributes": {"harness": harness, "session_file": rel, "prompts": len(prompts)},
                }
            )

    try:
        walk(root_fd)
    finally:
        os.close(root_fd)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", help="lowercase harness label (e.g. claude, agy, codex)")
    parser.add_argument("root", type=Path, help="local harness session directory")
    parser.add_argument("start", nargs="?", default="", help="inclusive window start (default: BF_START)")
    parser.add_argument("end", nargs="?", default="", help="exclusive window end (default: BF_END)")
    args = parser.parse_args()
    start_raw = args.start or os.environ.get("BF_START", "")
    end_raw = args.end or os.environ.get("BF_END", "")
    try:
        if not start_raw or not end_raw:
            raise InvalidError("START and END are required (positional or BF_START/BF_END env)")
        out = collect(
            args.harness,
            args.root,
            parse_window_iso(start_raw, "START"),
            parse_window_iso(end_raw, "END"),
        )
        payload = (json.dumps({"records": out}, ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise InvalidError("output exceeds 16 MiB")
        sys.stdout.buffer.write(payload)
    except InvalidError as error:
        sys.stderr.write(f"Agent telemetry collection failed: {error}.\n")
        sys.exit(1)
    except (OSError, UnicodeError, ValueError):
        sys.stderr.write(
            "Agent telemetry collection failed; check the session directory, permissions, and JSON logs.\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
