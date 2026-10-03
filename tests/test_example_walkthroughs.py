"""Execute the self-contained README walkthroughs against the installed checkout, without providers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def export() -> str:
    """The export lines the example brain documents: its walkthrough prints them after the action's answer."""
    readme = (ROOT / "examples/brain/README.md").read_text()
    return readme.split("The first three lines are:\n\n```text\n", 1)[1].split("\n```", 1)[0]


@pytest.mark.parametrize(
    ("example", "block", "expected"),
    [
        (
            "brain",
            None,
            ['"score":"16/16"', export(), '"state":"unchanged"', '"state":"changed"', '"brain":"example-team"'],
        ),
        ("retrieval", 0, ['"score":"23/23"', '"mrr":1.0', '"valid":true']),
        (
            "hooks",
            0,
            [
                "Brain context for repo:github.com/example/new-website",
                "Next task: Run the keyboard navigation check.",
                "- New website — Next actions (`projects/new-website.md#next-actions`)",
            ],
        ),
        ("routines", 0, ['"status":"ran"', "weekly-review-", '"valid":true']),
        (
            "routines",
            1,
            [
                "The first commit passed.",
                "The hook refused the second commit.",
                '"status":"failed"',
                '{"error":"broken link: plan.md","file":"projects/launch.md"}',
            ],
        ),
        (
            "team",
            0,
            [
                '{"notes":4,"problems":[],"records":3,"valid":true}',
                "CONFLICT (content): Merge conflict in memories/issues/",
                '{"error":"invalid JSON document","file":"memories/issues/',
                "Validation rejected the unresolved record.",
            ],
        ),
    ],
)
def test_readme_walkthrough(example: str, block: int | None, expected: list[str], tmp_path: Path) -> None:
    readme = (ROOT / "examples" / example / "README.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)\n```", readme, re.DOTALL)
    assert blocks
    # Most walkthroughs are one self-contained block; the example brain's blocks share one shell.
    script = "set -e\n" + ("\n".join(blocks) if block is None else blocks[block])
    # Dependencies are already installed by the owning test task. No resolution or download is needed.
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "UV_NO_SYNC": "1",
        "UV_OFFLINE": "1",
        "BF_BRAIN": str(tmp_path / "unrelated-brain"),
        "TMPDIR": str(tmp_path),
    }
    result = subprocess.run(  # noqa: S603 - execute reviewed repository instructions on their disposable fixtures
        ["bash", "--noprofile", "--norc", "-c", script],  # noqa: S607
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    for text in expected:
        assert text in result.stdout, result.stdout
    assert not (tmp_path / "unrelated-brain").exists()
    assert not list(tmp_path.glob("tmp.*")), "walkthrough did not remove its disposable brain"
