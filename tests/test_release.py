"""The release scripts guard irreversible steps: a tag exists only after CI succeeded on its exact commit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUSH_AND_TAG = ROOT / "scripts" / "push-and-tag.sh"
RELEASE_NOTES = ROOT / "scripts" / "release-notes.sh"
# A fake gh: CI lists run 42 unless FAKE_RUN is empty, watching it exits with FAKE_WATCH and it concludes with
# FAKE_CONCLUSION. They differ when the watch succeeds on a run that ended without success, such as a skipped one.
GH = """#!/bin/sh
case "$1 $2" in
  "run list") printf '%s\\n' "${FAKE_RUN-42}" ;;
  "run watch") exit "$FAKE_WATCH" ;;
  "run view") printf '%s\\n' "$FAKE_CONCLUSION" ;;
  *) echo "unexpected gh $*" >&2; exit 64 ;;
esac
"""


def git(folder: Path | None, *args: str) -> str:
    """Real Git on disposable repositories: the scripts' process boundary is the subject."""
    argv = ["git", *(["-C", str(folder)] if folder else []), *args]
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=30).stdout.strip()  # noqa: S603


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """A clone of a local origin whose main declares version 1.2.3."""
    origin = tmp_path / "origin.git"
    git(None, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    git(None, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "-B", "main")
    (clone / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.2.3"\n')
    git(clone, "add", "pyproject.toml")
    git(clone, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-q", "-m", "release")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "gh").write_text(GH)
    (tmp_path / "bin" / "gh").chmod(0o755)
    return clone


def push_and_tag(clone: Path, version: str = "1.2.3", **fake: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{clone.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "PUSH_AND_TAG_ATTEMPTS": "2",
        "PUSH_AND_TAG_DELAY": "0",
        "FAKE_WATCH": "0",
        "FAKE_CONCLUSION": "success",
        # An annotated tag names its tagger; the suite's isolated home has no Git identity.
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.test",
        **fake,
    }
    return subprocess.run(  # noqa: S603 - the repository's own release script against a local origin
        [str(PUSH_AND_TAG), version], cwd=clone, env=env, capture_output=True, text=True, timeout=60, check=False
    )


def remote_tags(clone: Path) -> str:
    return git(clone, "ls-remote", "--tags", "origin")


def test_push_and_tag_tags_the_pushed_commit_after_ci_succeeds(release: Path) -> None:
    result = push_and_tag(release)
    assert result.returncode == 0, result.stderr
    head = git(release, "rev-parse", "HEAD")
    assert git(release, "rev-parse", "origin/main") == head
    assert git(release, "rev-parse", "v1.2.3^{commit}") == head
    assert "refs/tags/v1.2.3" in remote_tags(release)
    # A tag never moves: a second run refuses before pushing anything, whether the tag is local or only on origin.
    again = push_and_tag(release)
    assert (again.returncode, "v1.2.3 already exists locally" in again.stderr) == (1, True), again.stderr
    git(release, "tag", "-d", "v1.2.3")
    remote = push_and_tag(release)
    assert (remote.returncode, "v1.2.3 already exists on origin" in remote.stderr) == (1, True), remote.stderr
    assert not git(release, "tag", "--list")


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        ({"FAKE_WATCH": "1", "FAKE_CONCLUSION": "failure"}, "did not succeed"),
        ({"FAKE_WATCH": "1", "FAKE_CONCLUSION": "cancelled"}, "did not succeed"),
        ({"FAKE_CONCLUSION": "skipped"}, "concluded skipped; nothing tagged"),
        ({"FAKE_RUN": ""}, "no CI run was listed"),
    ],
)
def test_push_and_tag_never_tags_without_a_successful_ci_run(release: Path, fake: dict[str, str], message: str) -> None:
    result = push_and_tag(release, **fake)
    assert result.returncode == 1
    assert message in result.stderr
    assert not git(release, "tag", "--list")
    assert not remote_tags(release)


def test_push_and_tag_checks_the_release_before_pushing(release: Path) -> None:
    for version, code, message in (("1.2", 2, "expected a version"), ("1.2.4", 1, "declares 1.2.3")):
        result = push_and_tag(release, version)
        assert (result.returncode, message in result.stderr) == (code, True), result.stderr
    git(release, "checkout", "-q", "-b", "feature")
    assert "release from main" in push_and_tag(release).stderr
    git(release, "checkout", "-q", "main")
    (release / "untracked.txt").write_text("work in progress\n")
    assert "uncommitted changes" in push_and_tag(release).stderr
    assert not git(release, "ls-remote", "--heads", "origin")


def test_a_failed_tag_push_deletes_the_local_tag_so_a_rerun_retries(release: Path) -> None:
    # Origin rejects tags, as a network drop or tag ruleset would, after accepting main.
    hook = release.parent / "origin.git" / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)  # Git copies hooks/ from its templates, which a minimal installation lacks.
    hook.write_text('#!/bin/sh\nwhile read -r _ _ ref; do case "$ref" in refs/tags/*) exit 1 ;; esac; done\n')
    hook.chmod(0o755)
    failed = push_and_tag(release)
    assert (failed.returncode, "pushing v1.2.3 failed" in failed.stderr) == (1, True), failed.stderr
    assert git(release, "rev-parse", "origin/main") == git(release, "rev-parse", "HEAD")
    assert not git(release, "tag", "--list")
    assert not remote_tags(release)
    hook.unlink()
    retried = push_and_tag(release)
    assert retried.returncode == 0, retried.stderr
    assert git(release, "rev-parse", "v1.2.3^{commit}") == git(release, "rev-parse", "HEAD")
    assert "refs/tags/v1.2.3" in remote_tags(release)


def test_release_notes_print_exactly_one_section(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## Unreleased\n\n## [v1.2.3](url) - 2026-09-29\n\nFixed.\n\n## [v1.2.2](url)\n\nOld.\n"
    )

    def notes(tag: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the repository's own release script
            [str(RELEASE_NOTES), tag, str(changelog)], capture_output=True, text=True, timeout=30, check=False
        )

    assert notes("v1.2.3").stdout == "\nFixed.\n\n"
    assert "no section for v9.9.9" in notes("v9.9.9").stderr
