"""The skill evaluation grades transcripts deterministically, and its cases name refs the example brain holds."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from conftest import ROOT

SCRIPT = ROOT / "scripts/eval_skills.py"
CASES = json.loads((ROOT / "scripts/eval_skills.json").read_text(encoding="utf-8"))["cases"]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_skills", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_skills"] = module  # dataclasses resolve annotations through the module registry
    spec.loader.exec_module(module)
    return module


EVAL = load()


def transcript(
    *uses: tuple[str, dict[str, Any]], answer: str = "", subtype: str = "success", denied: tuple[str, ...] = ()
) -> list[str]:
    """A stream-json transcript holding the given tool uses and final answer."""
    events: list[dict[str, Any]] = [{"type": "system", "subtype": "init", "skills": ["bf-use", "loop"]}]
    events += [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": arguments}]}}
        for name, arguments in uses
    ]
    events.append(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": False,
            "result": answer,
            "total_cost_usd": 0.01,
            "num_turns": 2,
            "permission_denials": [{"tool_name": name} for name in denied],
        }
    )
    return [json.dumps(event) for event in events]


CASE = {
    "name": "answer",
    "prompt": "Why?",
    "skill": "bf-use",
    "answer_all": ["disappear"],
    "answer_any": ["concepts/retention.md", "demo:retention"],
    "steps_all": [r"\bbf\s+read\b"],
    "read_only": True,
}


def test_a_session_that_follows_the_skill_passes(tmp_path: Path) -> None:
    session = EVAL.parse(
        transcript(
            ("Skill", {"skill": "bf-use"}),
            ("Bash", {"command": "bf read concepts/retention.md"}),
            answer="Upstream content can DISAPPEAR (concepts/retention.md).",
        )
    )
    assert session.available == ["bf-use", "loop"]
    assert (session.skills, session.cost, session.turns) == (["bf-use"], 0.01, 2)
    checks = EVAL.grade(CASE, session, tmp_path, [], None)
    assert checks == {
        "completed": True,
        "skill": True,
        "permitted": True,
        "forbidden": True,
        "answer": True,
        "steps": True,
        "read_only": True,
    }


def test_a_torn_last_line_is_ignored_but_a_corrupt_middle_line_fails() -> None:
    lines = transcript(("Skill", {"skill": "bf-use"}))
    killed = EVAL.parse([*lines[:2], lines[2][:20], ""])
    assert (killed.skills, killed.outcome) == (["bf-use"], "missing")
    # Line numbers count the transcript's own lines, blank ones included.
    with pytest.raises(EVAL.EvalError, match="transcript line 3 is not JSON"):
        EVAL.parse([lines[0], "", "not json", *lines[1:]])


def test_reading_the_skill_file_counts_as_invoking_it(tmp_path: Path) -> None:
    session = EVAL.parse(transcript(("Read", {"file_path": f"{tmp_path}/.claude/skills/bf-use/SKILL.md"})))
    assert EVAL.grade(CASE, session, tmp_path, [], None)["skill"]


def test_steps_match_commands_and_files_read(tmp_path: Path) -> None:
    case = {**CASE, "steps_all": [], "steps_any": [r"\bbf\s+status\b", r"logs/demo\.log"]}
    read = EVAL.parse(transcript(("Read", {"file_path": f"{tmp_path}/logs/demo.log"})))
    assert read.reads == [f"{tmp_path}/logs/demo.log"]
    assert EVAL.grade(case, read, tmp_path, [], None)["steps"]
    other = EVAL.parse(transcript(("Bash", {"command": "bf search demo"})))
    assert not EVAL.grade(case, other, tmp_path, [], None)["steps"]


def test_a_refused_tool_use_marks_the_trial_as_not_permitted(tmp_path: Path) -> None:
    session = EVAL.parse(transcript(("Bash", {"command": "S=x && python3 $S/a.py"}), denied=("Bash",)))
    assert session.denied == ["Bash"]
    assert not EVAL.grade(CASE, session, tmp_path, [], None)["permitted"]


@pytest.mark.parametrize(
    "use",
    [
        ("Bash", {"command": "bf status; bf collect demo --dry-run"}),
        ("Bash", {"command": "uv run --project . bf read"}),
        ("Bash", {"command": "bf queue 3 --retry"}),
        ("Write", {"file_path": "/brain/memories/demo/x.json", "content": "{}"}),
    ],
)
def test_forbidden_attempts_fail_even_when_refused(tmp_path: Path, use: tuple[str, dict[str, Any]]) -> None:
    checks = EVAL.grade(CASE, EVAL.parse(transcript(use)), tmp_path, [], None)
    assert not checks["forbidden"]


def test_outcome_checks_fail_on_wrong_answer_changes_and_errors(tmp_path: Path) -> None:
    session = EVAL.parse(transcript(answer="It is kept in concepts/retention.md.", subtype="error_max_turns"))
    checks = EVAL.grade(CASE, session, tmp_path, ["projects/example.md"], None)
    assert {name for name, ok in checks.items() if not ok} == {"completed", "skill", "answer", "steps", "read_only"}


def test_file_and_validation_checks_read_the_brain(tmp_path: Path) -> None:
    case = {
        "name": "save",
        "prompt": "Save",
        "skill": "bf-use",
        "files": [{"glob": "actions/*_cost/ACTION.md", "contains": "Storage"}],
        "validate": True,
    }
    action = tmp_path / "actions/2026-11-01_cost/ACTION.md"
    action.parent.mkdir(parents=True)
    action.write_text("measure storage\n", encoding="utf-8")
    session = EVAL.parse(transcript(("Skill", {"skill": "bf-use"})))
    assert EVAL.grade(case, session, tmp_path, ["actions/2026-11-01_cost/ACTION.md"], True)["files"]
    checks = EVAL.grade(case, session, tmp_path, ["projects/example.md"], False)
    assert not checks["files"]
    assert not checks["validate"]


def test_snapshots_see_content_and_mode_but_not_the_cache(tmp_path: Path) -> None:
    sensor = tmp_path / "sensors/demo.py"
    sensor.parent.mkdir()
    sensor.write_text("print()\n", encoding="utf-8")
    sensor.chmod(0o755)
    before = EVAL.snapshot(tmp_path)
    (tmp_path / ".bf").mkdir()
    (tmp_path / ".bf/index.sqlite").write_bytes(b"cache")
    assert EVAL.changes(before, EVAL.snapshot(tmp_path)) == []
    sensor.chmod(0o644)
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects/new.md").write_text("new\n", encoding="utf-8")
    assert EVAL.changes(before, EVAL.snapshot(tmp_path)) == ["projects/new.md", "sensors/demo.py"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_cases_name_packaged_skills_example_refs_and_valid_patterns(case: dict[str, Any]) -> None:
    assert case["skill"] in EVAL.SKILLS
    assert (ROOT / "src/bf/skills" / case["skill"] / "SKILL.md").is_file()
    for text in case.get("answer_all", []) + case.get("answer_any", []):
        if text.endswith(".md") or ".md#" in text:
            assert (ROOT / "examples/brain" / text.split("#")[0]).is_file(), text
    for pattern in case.get("steps_all", []) + case.get("steps_any", []):
        re.compile(pattern)
    for step in case.get("setup", []):
        assert "bf" in step or (ROOT / "examples/brain" / step["chmod"][0]).is_file()
