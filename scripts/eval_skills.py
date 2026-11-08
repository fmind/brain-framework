"""Run the packaged skills in headless Claude Code against copies of the fictional example brain and grade each session.

Each trial copies `examples/brain` into a fresh folder, collects its local demo sensor, installs the skills of this
checkout into the brain's `.claude/skills/` and asks one question from `scripts/eval_skills.json`. The session sees only
project settings and skills, no user or parent `CLAUDE.md`, the tools Bash, Read, Edit, Write, Glob, Grep and Skill, and
a PATH holding this checkout's `bf` plus `/usr/bin:/bin`. Bash may run `bf` and `python3`, and Claude Code's built-in
read-only commands; edits stay inside the brain. Grading is deterministic: the transcript (skill invoked, commands
attempted, files read, refusals, final answer) and the brain afterwards (changed files, `bf validate`). Grades and
transcripts stay under the run folder. The run needs Claude Code logged in and spends model tokens, so it is not part
of `mise run all`.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_suffix(".json")
EXAMPLE = "examples/brain"
SKILLS = ("bf-use", "bf-action", "bf-setup", "bf-maintain")
TOOLS = "Bash,Read,Edit,Write,Glob,Grep,Skill"
# What the skills reserve for explicit authority (collection, execution) or forbid (a brain's pinned runtime).
FORBIDDEN = re.compile(r"\bbf\s+(?:collect|run|update|watch|channel)\b|\bbf\s+queue\b[^\n]*--retry|\buv\s+run\b")
# Folders the cache, program logs and installed skills change; every other file counts as brain content.
UNTRACKED = frozenset({".bf", "logs", ".claude"})
# Variables a logged-in Claude Code may need; everything else, including provider tokens, stays out of the session.
KEEP = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "CLAUDE_CONFIG_DIR")
KEEP_PREFIXES = ("ANTHROPIC_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


class EvalError(Exception):
    """A setup or harness failure that is not the skills' fault; the message names the fix."""


@dataclass
class Session:
    """What one headless session did, read from its stream-json transcript."""

    available: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    answer: str = ""
    outcome: str = "missing"
    cost: float = 0.0
    turns: int = 0


def parse(lines: list[str]) -> Session:
    """Read a stream-json transcript; tool uses count whether or not permission was granted."""
    session = Session()
    last = max((index for index, line in enumerate(lines) if line.strip()), default=-1)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            if index == last:
                break  # a session killed at --timeout can tear its last line, even before trailing blank lines
            raise EvalError(f"transcript line {index + 1} is not JSON: {error.msg}") from error
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            session.available = [str(name) for name in event.get("skills") or []]
        elif kind == "assistant":
            for item in event.get("message", {}).get("content") or []:
                if item.get("type") == "tool_use":
                    use(session, str(item.get("name")), item.get("input") or {})
        elif kind == "result":
            session.answer = str(event.get("result") or "")
            session.outcome = (
                "success"
                if event.get("subtype") == "success" and not event.get("is_error")
                else str(event.get("subtype"))
            )
            session.cost = float(event.get("total_cost_usd") or 0.0)
            session.turns = int(event.get("num_turns") or 0)
            session.denied = [str(denial.get("tool_name")) for denial in event.get("permission_denials") or []]
    return session


def use(session: Session, name: str, arguments: dict[str, Any]) -> None:
    """Record one tool use: a skill launch or `SKILL.md` read, a file read, a shell command or a file write."""
    if name == "Skill":
        session.skills.append(str(arguments.get("skill") or arguments.get("command") or "").lstrip("/"))
    elif name == "Read":
        session.reads.append(path := str(arguments.get("file_path", "")))
        if path.endswith("/SKILL.md"):
            session.skills.append(Path(path).parent.name)
    elif name == "Bash":
        session.commands.append(str(arguments.get("command", "")))
    elif name in {"Edit", "Write", "NotebookEdit"}:
        session.writes.append(str(arguments.get("file_path") or arguments.get("notebook_path") or ""))


def snapshot(brain: Path) -> dict[str, str]:
    """Digest and mode of every brain file outside the cache, logs and installed skills."""
    files: dict[str, str] = {}
    for folder, dirs, names in os.walk(brain):
        here = Path(folder)
        if here == brain:
            dirs[:] = [name for name in dirs if name not in UNTRACKED]
        for name in names:
            path = here / name
            files[path.relative_to(brain).as_posix()] = (
                f"{path.lstat().st_mode:o}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            )
    return files


def changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths added, removed or changed in content or mode."""
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def grade(
    case: dict[str, Any], session: Session, brain: Path, changed: list[str], valid: bool | None
) -> dict[str, bool]:
    """Every check the case declares, by name; a trial passes when all hold."""
    answer = session.answer.casefold()
    checks = {
        "completed": session.outcome == "success",
        "skill": case["skill"] in session.skills,
        # A refused tool use is the harness boundary at work, not the skill: such a trial says little about the skill.
        "permitted": not session.denied,
        # Attempts count even when the permission boundary refused them.
        "forbidden": not any(FORBIDDEN.search(command) for command in session.commands)
        and not any("/memories/" in path or path.startswith("memories/") for path in session.writes),
    }
    if "answer_all" in case or "answer_any" in case:
        checks["answer"] = all(text.casefold() in answer for text in case.get("answer_all", [])) and (
            "answer_any" not in case or any(text.casefold() in answer for text in case["answer_any"])
        )
    if "steps_all" in case or "steps_any" in case:
        # A step is a Bash command or a file the Read tool opened.
        steps = session.commands + session.reads

        def took(pattern: str) -> bool:
            return any(re.search(pattern, step) for step in steps)

        checks["steps"] = all(took(pattern) for pattern in case.get("steps_all", [])) and (
            "steps_any" not in case or any(took(pattern) for pattern in case["steps_any"])
        )
    if "files" in case:
        checks["files"] = all(
            any(
                fnmatch.fnmatchcase(path, spec["glob"])
                and (brain / path).is_file()
                and spec.get("contains", "").casefold() in (brain / path).read_text(encoding="utf-8").casefold()
                for path in changed
            )
            for spec in case["files"]
        )
    if case.get("read_only"):
        checks["read_only"] = not changed
    if case.get("validate"):
        checks["validate"] = valid is True
    return checks


def isolated_env(trial: Path, bin_dir: Path) -> dict[str, str]:
    """The session's environment: login and proxy settings, this checkout's `bf`, and bf state inside the trial."""
    env = {name: value for name in KEEP if (value := os.environ.get(name))}
    env |= {name: value for name, value in os.environ.items() if name.startswith(KEEP_PREFIXES)}
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        env[name] = str(trial / "xdg" / name.removeprefix("XDG_").removesuffix("_HOME").lower())
    return env


def bf(env: dict[str, str], brain: Path, *args: str, fails: bool | None = False) -> dict[str, Any]:
    """Run this checkout's `bf` in the brain and return its JSON reply; `fails=None` accepts either outcome."""
    result = subprocess.run(  # noqa: S603 - this checkout's bf with fixed arguments
        [str(ROOT / ".venv/bin/bf"), *args],
        cwd=brain,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if fails is not None and (result.returncode != 0) != fails:
        raise EvalError(f"setup: bf {' '.join(args)} exited {result.returncode}: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def excludes(brain: Path) -> list[str]:
    """Instruction files outside the brain that Claude Code would otherwise load into the session."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    paths = [config / "CLAUDE.md"]
    for parent in brain.parents:
        paths += [
            parent / "CLAUDE.md",
            parent / "CLAUDE.local.md",
            parent / "AGENTS.md",
            parent / ".claude" / "CLAUDE.md",
        ]
    return [str(path) for path in paths]


def prepare(case: dict[str, Any], trial: Path) -> tuple[Path, dict[str, str]]:
    """A fresh example brain with its demo records collected, the case setup applied and the skills installed."""
    brain = trial / "brain"
    tracked = subprocess.run(  # noqa: S603 - git lists the example's tracked files
        [shutil.which("git") or "git", "-C", str(ROOT), "ls-files", "-z", EXAMPLE], capture_output=True, check=True
    ).stdout.decode()
    for name in filter(None, tracked.split("\0")):
        target = brain / Path(name).relative_to(EXAMPLE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    bin_dir = trial / "bin"
    bin_dir.mkdir()
    (bin_dir / "bf").symlink_to(ROOT / ".venv/bin/bf")
    env = isolated_env(trial, bin_dir)
    if not bf(env, brain, "skills", str(brain / ".claude/skills")).get("ok"):
        raise EvalError("setup: bf skills did not install every skill")
    bf(env, brain, "update")
    for step in case.get("setup", []):
        if "chmod" in step:
            path, mode = step["chmod"]
            (brain / path).chmod(int(mode, 8))
        else:
            bf(env, brain, *step["bf"], fails=step.get("fails", False))
    return brain, env


def session_argv(claude: str, model: str, budget: float, brain: Path, prompt: str) -> list[str]:
    """Headless Claude Code limited to project settings, the brain's skills and a deny-by-default tool boundary."""
    settings = {"claudeMdExcludes": excludes(brain), "autoMemoryEnabled": False}
    allowed = [
        "Bash(bf:*)",
        "Bash(python3:*)",
        "Read",
        "Glob",
        "Grep",
        "Skill",
        f"Edit(/{brain}/**)",
        f"Write(/{brain}/**)",
    ]
    return [
        claude, "-p", prompt, "--model", model, "--setting-sources", "project", "--settings", json.dumps(settings),
        "--strict-mcp-config", "--tools", TOOLS, "--allowedTools", *allowed, "--permission-mode", "dontAsk",
        "--max-budget-usd", f"{budget}", "--no-session-persistence", "--output-format", "stream-json", "--verbose",
    ]  # fmt: skip


def run_trial(case: dict[str, Any], trial: Path, args: argparse.Namespace, user_skills: set[str]) -> dict[str, Any]:
    """Prepare, run and grade one trial; the transcript and grade stay in the trial folder."""
    trial.mkdir(parents=True)
    brain, env = prepare(case, trial)
    before = snapshot(brain)
    transcript = trial / "transcript.jsonl"
    started = time.monotonic()
    with transcript.open("w") as out, (trial / "claude.stderr").open("w") as err:
        process = subprocess.Popen(  # noqa: S603 - Claude Code with fixed arguments and no shell
            session_argv(args.claude, args.model, args.budget, brain, case["prompt"]),
            cwd=brain, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True,
        )  # fmt: skip
        try:
            process.wait(timeout=args.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            timed_out = True
    seconds = round(time.monotonic() - started, 1)
    session = parse(transcript.read_text(encoding="utf-8").splitlines())
    if session.outcome == "missing" and not timed_out:
        error = (trial / "claude.stderr").read_text(encoding="utf-8").strip()[:300]
        raise EvalError(
            f"Claude Code exited {process.returncode} without a result ({error}); log in with `claude` and retry"
        )
    if leaked := sorted(set(session.available) & user_skills):
        raise EvalError(
            f"user-level skills reached the session ({', '.join(leaked[:5])}); check --setting-sources project"
        )
    changed = changes(before, snapshot(brain))
    valid = bf(env, brain, "validate", fails=None).get("valid") if case.get("validate") else None
    checks = grade(case, session, brain, changed, valid)
    if timed_out:
        checks["completed"] = False
    result = {
        "case": case["name"],
        "passed": all(checks.values()),
        "failed": sorted(name for name, ok in checks.items() if not ok),
        "skills": session.skills,
        "commands": session.commands,
        "reads": session.reads,
        "denied": session.denied,
        "changed": changed,
        "answer": session.answer,
        "cost_usd": round(session.cost, 4),
        "turns": session.turns,
        "seconds": seconds,
        "timed_out": timed_out,
    }
    (trial / "grade.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # As bf does, ignore a relative XDG_CACHE_HOME.
    configured = os.environ.get("XDG_CACHE_HOME", "")
    cache = Path(configured) if configured.startswith("/") else Path.home() / ".cache"
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Claude Code model alias or name (default: sonnet; haiku rarely loads the skills)",
    )
    parser.add_argument("--trials", type=int, default=1, help="Fresh trials per case (default: 1)")
    parser.add_argument("--case", action="append", default=[], help="Run only this case; repeatable")
    parser.add_argument("--threshold", type=float, default=1.0, help="Minimum pass rate per case (default: 1.0)")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per session (default: 300)")
    parser.add_argument("--budget", type=float, default=1.0, help="Maximum USD per session (default: 1.0)")
    parser.add_argument("--jobs", type=int, default=3, help="Concurrent sessions (default: 3)")
    parser.add_argument("--dir", type=Path, default=cache / "bf-skill-eval", help="Parent of the run folder")
    parser.add_argument("--claude", default=shutil.which("claude"), help="Claude Code executable (default: from PATH)")
    args = parser.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    if unknown := set(args.case) - {case["name"] for case in cases}:
        parser.error(f"--case: unknown {', '.join(sorted(unknown))}; see {CASES.name}")
    if args.trials < 1 or args.jobs < 1 or not 0 <= args.threshold <= 1:
        parser.error("--trials and --jobs must be at least 1 and --threshold between 0 and 1")
    if not args.claude:
        parser.error("--claude: Claude Code is not on PATH; install it or pass its path")
    if not (ROOT / ".venv/bin/bf").is_file():
        sys.stderr.write("eval: .venv/bin/bf is missing; run `uv sync --locked` first\n")
        return 1
    selected = [case for case in cases if not args.case or case["name"] in args.case]
    run_dir = args.dir.expanduser().resolve() / time.strftime("%Y%m%dT%H%M%S")
    user_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "skills"
    user_skills = {path.name for path in user_dir.iterdir()} - set(SKILLS) if user_dir.is_dir() else set()
    jobs = [(case, run_dir / f"{case['name']}-{n}") for case in selected for n in range(1, args.trials + 1)]
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(lambda job: run_trial(*job, args, user_skills), jobs))
    except EvalError as error:
        sys.stderr.write(f"eval: {error}; trial folders are under {run_dir}\n")
        return 1
    summary_cases = []
    for case in selected:
        trials = [result for result in results if result["case"] == case["name"]]
        rate = sum(result["passed"] for result in trials) / len(trials)
        failures = [result["failed"] for result in trials if not result["passed"]]
        summary_cases.append({"name": case["name"], "pass_rate": rate, "failures": failures})
    summary = {
        "model": args.model,
        "trials": args.trials,
        "threshold": args.threshold,
        "ok": all(case["pass_rate"] >= args.threshold for case in summary_cases),
        "cases": summary_cases,
        "cost_usd": round(sum(result["cost_usd"] for result in results), 4),
        "seconds": round(time.monotonic() - started, 1),
        "run": str(run_dir),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(summary, separators=(",", ":")) + "\n")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
