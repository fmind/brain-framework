#!/usr/bin/env python3
"""Collect coding-agent sessions with the owner's prompts from the normalized dot session store."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MAX_SESSIONS = 20_000
MAX_MANIFEST = 64 << 10
MAX_TRANSCRIPT = 16 << 20
MAX_OUTPUT = 16 << 20
MAX_PROMPT = 2_000
MAX_TEXT = 64 << 10
MAX_TITLE = 120
GITHUB_PART = re.compile(r"^[A-Za-z0-9._-]+$")
# Harness-injected turns and wrappers are not the owner's requests.
INJECTED_PREFIXES = (
    "# AGENTS.md instructions for",
    "Are you still working on",
    "Your claude.ai usage limit",
    "Caveat: The messages below are auto-generated",
    "[Request interrupted",
    "This session is being continued from a previous",
    "API Error:",
    "<system>",
)
HARNESS_TAGS = (
    "system-reminder",
    "ADDITIONAL_METADATA",
    "USER_SETTINGS_CHANGE",
    "task-notification",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
    "recommended_plugins",
    "environment_context",
    "user-prompt-submit-hook",
    "ide_opened_file",
    "ide_selection",
    "codex_internal_context",
)


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must carry a timezone")
    return parsed


def prompt(content: object) -> str:
    """Return the owner's request without harness wrappers, or an empty string for injected turns."""
    text = content if isinstance(content, str) else ""
    for tag in HARNESS_TAGS:
        if f"<{tag}" in text:
            text = re.sub(rf"<{re.escape(tag)}(?:\s[^>]*)?>.*?</{re.escape(tag)}>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?USER_REQUEST>", "", text).strip()
    return "" if text.startswith(INJECTED_PREFIXES) else text


def bounded_prompt(text: str) -> str:
    return text if len(text) <= MAX_PROMPT else text[:MAX_PROMPT] + " […]"


def repository_name(remote: str) -> str:
    """Return owner/name for a GitHub remote in HTTPS or SSH form, or an empty string."""
    if not remote or "?" in remote or "#" in remote:
        return ""
    path = ""
    if "://" in remote:
        parsed = urlsplit(remote)
        if (parsed.hostname or "").lower() != "github.com":
            return ""
        path = parsed.path.removeprefix("/")
    elif re.match(r"^[^/:]+:[^/]+/", remote):
        authority, path = remote.split(":", 1)
        if authority.rsplit("@", 1)[-1].lower() != "github.com":
            return ""
    parts = path.removesuffix(".git").split("/")
    if len(parts) != 2 or any(part in {"", ".", ".."} or not GITHUB_PART.fullmatch(part) for part in parts):
        return ""
    return "/".join(parts)


def remote_repository(cwd: str) -> str:
    """Resolve the GitHub repository of a still-existing checkout offline; tolerate any failure."""
    if not cwd or not Path(cwd).is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    if result.returncode != 0 or len(result.stdout) > 4096:
        return ""
    return repository_name(result.stdout.decode(errors="replace").strip())


def local_repository(cwd: str, home: Path) -> str:
    """Name the checkout by its path below HOME, else by its last component."""
    path = PurePosixPath(cwd)
    try:
        return path.relative_to(home.as_posix()).as_posix()
    except ValueError:
        return path.name


def read_json(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds its byte limit")
    return data


def identity(manifest: dict[str, object]) -> tuple[str, str]:
    agent, session_id = manifest.get("agent"), manifest.get("session_id")
    if not isinstance(agent, str) or not isinstance(session_id, str) or not agent or not session_id:
        raise ValueError("manifest lacks its identity")
    return agent, session_id


def rank(manifest: dict[str, object]) -> tuple[str, int, str]:
    """The store keeps one directory per ingestion; the newest snapshot is a superset of the older ones."""
    count = manifest.get("record_count", 0)
    return (
        str(manifest.get("high_water_mark", "")),
        count if isinstance(count, int) else 0,
        str(manifest.get("ingested_at", "")),
    )


def session(
    directory: Path,
    manifest: dict[str, object],
    home: Path,
    start: datetime,
    end: datetime,
    remotes: dict[str, str],
) -> dict[str, object] | None:
    agent, session_id = identity(manifest)
    # Subagent threads carry delegation prompts written by another agent, not by the owner.
    if session_id.startswith("agent-") or instant(manifest.get("high_water_mark")) < start:
        return None
    turns: list[tuple[str, str]] = []
    cwd = ""
    for line in read_json(directory / "transcript.jsonl", MAX_TRANSCRIPT).splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("transcript line is not an object")
        if isinstance(record.get("cwd"), str) and record["cwd"] and not cwd:
            cwd = record["cwd"]
        if record.get("role") != "user":
            continue
        when = instant(record.get("ts"))
        if not start <= when < end:
            continue
        text = prompt(record.get("content"))
        if text:
            turns.append((record["ts"], text))
    if not turns:
        return None
    turns.sort(key=lambda turn: turn[0])
    first_line = next((line.strip() for line in turns[0][1].splitlines() if line.strip()), "")
    if len(first_line) > MAX_TITLE:
        first_line = first_line[: MAX_TITLE - 1] + "…"
    local = local_repository(cwd, home) if cwd else ""
    if cwd not in remotes:
        remotes[cwd] = remote_repository(cwd)
    links = ([f"repo:local/{local}"] if local else []) + ([f"repo:github.com/{remotes[cwd]}"] if remotes[cwd] else [])
    lines = [
        f"Agent: {agent}",
        f"Session: {session_id}",
        f"Started: {turns[0][0]}",
        f"Directory: {cwd or 'unknown'}",
        f"Repository: {', '.join(links) or 'unknown'}",
        "",
    ]
    length = sum(len(line) + 1 for line in lines)
    for index, (when, text) in enumerate(turns):
        entry = f"[{when}] {bounded_prompt(text)}\n"
        if length + len(entry) > MAX_TEXT:
            lines.append(f"[truncated: {len(turns) - index} more prompts]")
            break
        lines.append(entry)
        length += len(entry) + 1
    return {
        "id": f"{agent}:{session_id}",
        "title": f"{agent} in {PurePosixPath(cwd).name if cwd else '?'}: {first_line or 'untitled request'}",
        "time": turns[0][0],
        "text": "\n".join(lines).rstrip("\n"),
        "url": "",
        "links": links,
        "aliases": [f"session:{agent}/{session_id}"],
        "attributes": {
            "agent": agent,
            "session_id": session_id,
            "source_type": manifest.get("source_type", ""),
            "cwd": cwd,
            "branch": "",
            "turns": len(turns),
            "completeness": manifest.get("completeness", ""),
        },
    }


def directories(parent: Path) -> list[Path]:
    """Child directories that are not symlinks, sorted for reproducible output."""
    with os.scandir(parent) as entries:
        return sorted(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))


def collect(root: Path, home: Path, start: datetime, end: datetime) -> list[dict[str, object]]:
    if start >= end:
        raise ValueError("start must be earlier than end")
    if not root.is_dir() or root.is_symlink():
        raise ValueError("session store root is not a directory")
    newest: dict[tuple[str, str], tuple[tuple[str, int, str], Path, dict[str, object]]] = {}
    examined = 0
    for agent_dir in directories(root):
        for lineage_dir in directories(agent_dir):
            for session_dir in directories(lineage_dir):
                examined += 1
                if examined > MAX_SESSIONS:
                    raise ValueError("session store exceeds 20000 sessions")
                path = session_dir / "manifest.json"
                if not path.is_file() or path.is_symlink():
                    continue
                manifest = json.loads(read_json(path, MAX_MANIFEST))
                if not isinstance(manifest, dict):
                    raise ValueError("manifest is not an object")
                key, order = identity(manifest), rank(manifest)
                if key not in newest or order > newest[key][0]:
                    newest[key] = (order, session_dir, manifest)
    records: list[dict[str, object]] = []
    remotes: dict[str, str] = {}
    for _order, session_dir, manifest in newest.values():
        record = session(session_dir, manifest, home, start, end, remotes)
        if record is not None:
            records.append(record)
    return sorted(records, key=lambda record: (str(record["time"]), str(record["id"])))


if __name__ == "__main__":
    try:
        home = Path(os.environ["HOME"])
        root = Path(sys.argv[3]) if len(sys.argv) > 3 else home / ".agents/sessions/v1"
        payload = json.dumps(
            collect(root, home, instant(sys.argv[1]), instant(sys.argv[2])), ensure_ascii=False, allow_nan=False
        )
        if len(payload.encode()) > MAX_OUTPUT:
            raise ValueError("normalized sessions exceed 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError, RecursionError:
        print("Agent session collection failed; check the session store and the requested window.", file=sys.stderr)
        sys.exit(1)
