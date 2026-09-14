"""The agent-sessions adapter projects the owner's prompts from a synthetic normalized session store."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conftest import Provider

CLAUDE_SID = "11111111-2222-4333-8444-555555555555"
CODEX_SID = "019f0000-aaaa-7bbb-8ccc-dddddddddddd"
START, END = "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z"


def turn(agent: str, sid: str, role: str, ts: str, content: str, cwd: str | None, model: str | None = None) -> str:
    record: dict[str, object] = {"ts": ts, "agent": agent, "sid": sid, "role": role, "content": content}
    if cwd is not None:
        record["cwd"] = cwd
    if model is not None:
        record["model"] = model
    return json.dumps(record, ensure_ascii=False)


def write_session(
    root: Path, agent: str, sid: str, source_type: str, lines: list[str], high_water: str, lineage: str = "a" * 64
) -> Path:
    directory = root / agent / lineage / (sid.replace("-", "") + "0" * 32)[:64]
    directory.mkdir(parents=True)
    (directory / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {
        "parser_version": "1",
        "agent": agent,
        "session_id": sid,
        "lineage_id": lineage,
        "source_type": source_type,
        "high_water_mark": high_water,
        "completeness": "complete",
        "schema_version": 1,
        "record_count": len(lines),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def checkout(path: Path, remote: str) -> None:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(path.parent / "gitconfig")}
    for args in (["init", "-q"], ["remote", "add", "origin", remote]):
        # Real git answers the offline remote lookup this adapter performs; nothing leaves the machine.
        subprocess.run(["git", "-C", str(path), *args], env=env, check=True, capture_output=True)  # noqa: S603,S607


def store(home: Path) -> Path:
    root = home / ".agents/sessions/v1"
    project = home / "fmind" / "project"
    checkout(project, "git@github.com:owner/project.git")
    cwd = str(project)
    write_session(
        root,
        "claude",
        CLAUDE_SID,
        "claude-jsonl",
        [
            turn("claude", CLAUDE_SID, "user", "2026-09-01T23:59:59.000Z", "Yesterday's request.", cwd, "claude-x"),
            turn("claude", CLAUDE_SID, "assistant", "2026-09-02T00:00:01.000Z", "Reply.", cwd, "claude-x"),
            turn(
                "claude",
                CLAUDE_SID,
                "user",
                "2026-09-02T08:00:00.000Z",
                "<system-reminder>injected</system-reminder>Fix the résumé page 日本語\nSecond line.",
                cwd,
                "claude-x",
            ),
            turn("claude", CLAUDE_SID, "assistant", "2026-09-02T08:00:05.000Z", "Working.", cwd, "claude-x"),
            turn("claude", CLAUDE_SID, "user", "2026-09-02T09:00:00.000Z", "x" * 5000, cwd, "claude-x"),
            turn(
                "claude",
                CLAUDE_SID,
                "user",
                "2026-09-02T09:30:00.000Z",
                "This session is being continued from a previous conversation.",
                cwd,
                "claude-x",
            ),
        ],
        "2026-09-02T09:30:00.000Z",
    )
    write_session(
        root,
        "codex",
        CODEX_SID,
        "codex-jsonl",
        [
            turn("codex", CODEX_SID, "user", "2026-09-02T10:00:00Z", "Ship it.", "/srv/elsewhere/gone", "gpt-x"),
            turn("codex", CODEX_SID, "assistant", "2026-09-02T10:00:09Z", "Done.", "/srv/elsewhere/gone", "gpt-x"),
        ],
        "2026-09-02T10:00:09Z",
        lineage="b" * 64,
    )
    write_session(
        root,
        "codex",
        "019f0000-aaaa-7bbb-8ccc-000000000000",
        "codex-jsonl",
        [turn("codex", "019f0000-aaaa-7bbb-8ccc-000000000000", "user", "2026-08-30T10:00:00Z", "Old.", cwd)],
        "2026-08-30T10:00:00Z",
        lineage="c" * 64,
    )
    write_session(
        root,
        "claude",
        "agent-a1b2c3d4e5f6a7b8c9d0e",
        "claude-jsonl",
        [turn("claude", "agent-a1b2c3d4e5f6a7b8c9d0e", "user", "2026-09-02T11:00:00.000Z", "Delegated task.", cwd)],
        "2026-09-02T11:00:00.000Z",
        lineage="d" * 64,
    )
    write_session(
        root,
        "agy",
        "22222222-3333-4444-8555-666666666666",
        "antigravity-jsonl",
        [turn("agy", "22222222-3333-4444-8555-666666666666", "user", "2026-09-02T12:00:00Z", "No directory.", None)],
        "2026-09-02T12:00:00Z",
        lineage="e" * 64,
    )
    outside = home / "outside-store"
    write_session(
        outside,
        "grok",
        "33333333-4444-4555-8666-777777777777",
        "grok-jsonl",
        [turn("grok", "33333333-4444-4555-8666-777777777777", "user", "2026-09-02T13:00:00Z", "Linked in.", cwd)],
        "2026-09-02T13:00:00Z",
    )
    (root / "grok").mkdir()
    (root / "grok" / ("f" * 64)).symlink_to(outside / "grok" / ("a" * 64), target_is_directory=True)
    return root


def test_sessions_project_prompts_links_and_bounds(provider: Provider, tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = store(home)
    records = provider.records("agent-sessions.py", START, END, str(root), home=home)
    assert [record.id for record in records] == [
        f"claude:{CLAUDE_SID}",
        f"codex:{CODEX_SID}",
        "agy:22222222-3333-4444-8555-666666666666",
    ]
    claude = records[0]
    assert claude.title == "claude in project: Fix the résumé page 日本語"
    assert claude.time == "2026-09-02T08:00:00.000000Z"
    assert claude.links == ["repo:github.com/owner/project", "repo:local/fmind/project"]
    assert claude.aliases == [f"session:claude/{CLAUDE_SID}"]
    assert "Yesterday's request." not in claude.text
    assert "injected" not in claude.text
    assert "being continued" not in claude.text
    assert "[2026-09-02T08:00:00.000Z] Fix the résumé page 日本語\nSecond line." in claude.text
    assert "x" * 2000 + " […]" in claude.text
    assert "x" * 2001 not in claude.text
    assert claude.text.startswith(f"Agent: claude\nSession: {CLAUDE_SID}\nStarted: 2026-09-02T08:00:00.000Z\n")
    assert claude.attributes == {
        "agent": "claude",
        "session_id": CLAUDE_SID,
        "source_type": "claude-jsonl",
        "cwd": str(home / "fmind" / "project"),
        "branch": "",
        "turns": 2,
        "completeness": "complete",
    }
    codex = records[1]
    assert codex.title == "codex in gone: Ship it."
    assert codex.links == ["repo:local/gone"]
    assert records[2].title == "agy in ?: No directory."
    assert records[2].links == []
    assert "Directory: unknown" in records[2].text
    assert provider.records("agent-sessions.py", START, END, home=home) == records
    assert provider.records("agent-sessions.py", "2026-09-03T00:00:00Z", "2026-09-04T00:00:00Z", home=home) == []


def test_multi_day_session_is_reobserved_per_window(provider: Provider, tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = store(home)
    earlier = provider.records("agent-sessions.py", "2026-09-01T00:00:00Z", END, str(root), home=home)
    claude = next(record for record in earlier if record.id.startswith("claude:"))
    assert claude.time == "2026-09-01T23:59:59.000000Z"
    assert claude.title == "claude in project: Yesterday's request."
    assert claude.attributes["turns"] == 3


def test_sessions_fail_closed_on_damage_and_missing_root(provider: Provider, tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = store(home)
    assert provider.run("agent-sessions.py", START, END, str(tmp_path / "absent"), home=home).returncode == 1
    assert provider.run("agent-sessions.py", END, START, str(root), home=home).returncode == 1
    transcript = next(root.glob("claude/*/*/transcript.jsonl"))
    transcript.write_text(transcript.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    result = provider.run("agent-sessions.py", START, END, str(root), home=home)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Agent session collection failed; check the session store and the requested window.\n"


def test_repeated_ingestions_of_one_session_yield_its_newest_snapshot(provider: Provider, tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".agents/sessions/v1"
    sid = "01a0dup0-0000-7000-8000-000000000001"
    older = [turn("codex", sid, "user", "2026-09-02T10:00:00Z", "First request.", "/work/project")]
    newer = [
        *older,
        turn("codex", sid, "assistant", "2026-09-02T10:00:05Z", "Reply.", "/work/project"),
        turn("codex", sid, "user", "2026-09-02T10:01:00Z", "Second request.", "/work/project"),
    ]
    # The store keeps one directory per ingestion of the same session; only the newest snapshot counts.
    write_session(root, "codex", sid, "codex-jsonl", newer, "2026-09-02T10:01:00Z", lineage="f" * 64)
    write_session(root, "codex", sid, "codex-jsonl", older, "2026-09-02T10:00:00Z", lineage="g" * 64)
    records = provider.records(
        "agent-sessions.py", "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z", str(root), home=home
    )
    assert [record.id for record in records] == [f"codex:{sid}"]
    assert records[0].attributes["turns"] == 2
    assert "Second request." in records[0].text
