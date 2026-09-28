"""Execute the self-contained README walkthroughs against the installed checkout, without providers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        ("brain", ['"score":"16/16"', '"state":"unchanged"', '"state":"changed"', '"brain":"example-team"']),
        ("context-hub", ['"score":"10/10"', "projects/new-website.md#launch-review", "Hold public launch"]),
        ("retrieval", ['"score":"14/14"', '"valid":true']),
        (
            "hooks",
            ["Brain context for repo:github.com/example/new-website", "Next task: Run the keyboard navigation check."],
        ),
        ("routines", ['"status":"ran"', "weekly-review-", '"valid":true']),
        (
            "team",
            [
                '{"notes":4,"problems":[],"records":3,"valid":true}',
                "CONFLICT (content): Merge conflict in memories/issues/",
                '{"error":"invalid JSON document","file":"memories/issues/',
                "Validation rejected the unresolved record.",
            ],
        ),
    ],
)
def test_readme_walkthrough(example: str, expected: list[str], tmp_path: Path) -> None:
    readme = (ROOT / "examples" / example / "README.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)\n```", readme, re.DOTALL)
    assert blocks
    script = "set -e\n" + ("\n".join(blocks) if example == "brain" else blocks[0])
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
