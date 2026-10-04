"""The release scripts guard irreversible steps: a tag exists only after CI succeeded on its exact commit."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bf import __version__

ROOT = Path(__file__).resolve().parents[1]
PUSH_AND_TAG = ROOT / "scripts" / "push-and-tag.sh"
RELEASE_NOTES = ROOT / "scripts" / "release-notes.sh"
VERIFY_RELEASE = ROOT / "scripts" / "verify-release.sh"
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


WHEEL = f"brain_framework-{__version__}-py3-none-any.whl"
SDIST = f"brain_framework-{__version__}.tar.gz"
# Fakes for verify-release: gh describes the release from FAKE_* variables and downloads two small assets, curl
# prints FAKE_PYPI unless FAKE_PYPI_MISSING is set, and uv installs the downloaded wheel, unless FAKE_UV_FAIL is set,
# as a bf that runs this checkout.
VERIFY_GH = """#!/bin/sh
case "$1 $2" in
  "release view") printf '%s\\t%s\\t%s\\t%s\\n' "${FAKE_DRAFT-false}" false https://example.test/release "$FAKE_ASSETS" ;;
  "release download")
    while [ "$#" -gt 0 ]; do [ "$1" = --dir ] && dir=$2; shift; done
    mkdir -p "$dir" && printf wheel > "$dir/$FAKE_WHEEL" && printf sdist > "$dir/$FAKE_SDIST" ;;
  "attestation verify") exit "${FAKE_ATTESTATION-0}" ;;
  *) echo "unexpected gh $*" >&2; exit 64 ;;
esac
"""
CURL = """#!/bin/sh
[ -n "${FAKE_PYPI_MISSING-}" ] && exit 22
printf '%s\\n' "$FAKE_PYPI"
"""
UV = """#!/bin/sh
for wheel; do :; done
case "$wheel" in */"$FAKE_WHEEL") ;; *) echo "unexpected install $wheel" >&2; exit 64 ;; esac
[ -n "${FAKE_UV_FAIL-}" ] && exit 1
mkdir -p "$UV_TOOL_BIN_DIR"
printf '#!/bin/sh\\nexec "%s" -m bf "$@"\\n' "$FAKE_PYTHON" > "$UV_TOOL_BIN_DIR/bf"
chmod +x "$UV_TOOL_BIN_DIR/bf"
"""


def pypi(wheel: str = "wheel", sdist: str = "sdist") -> str:
    """PyPI's JSON for the release, with the digests of the given asset contents."""
    files = ((WHEEL, wheel), (SDIST, sdist))
    urls = [
        {"filename": name, "digests": {"sha256": hashlib.sha256(text.encode()).hexdigest()}} for name, text in files
    ]
    return json.dumps({"urls": urls})


@pytest.fixture
def published(tmp_path: Path) -> Path:
    """A clone whose origin holds the annotated tag of this checkout's version, beside fake gh, curl and uv."""
    origin = tmp_path / "origin.git"
    git(None, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    git(None, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "-B", "main")
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.test")
    git(clone, *identity, "commit", "-q", "--allow-empty", "-m", "release")
    git(clone, *identity, "tag", "-a", f"v{__version__}", "-m", "release")
    git(clone, "push", "-q", "--follow-tags", "origin", "main")
    (tmp_path / "bin").mkdir()
    for name, text in (("gh", VERIFY_GH), ("curl", CURL), ("uv", UV)):
        (tmp_path / "bin" / name).write_text(text)
        (tmp_path / "bin" / name).chmod(0o755)
    return clone


def verify_release(clone: Path, version: str = __version__, **fake: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{clone.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "XDG_CACHE_HOME": str(clone.parent / "cache"),
        "FAKE_ASSETS": f"{WHEEL} {SDIST}",
        "FAKE_WHEEL": WHEEL,
        "FAKE_SDIST": SDIST,
        "FAKE_PYPI": pypi(),
        "FAKE_PYTHON": sys.executable,
        **fake,
    }
    return subprocess.run(  # noqa: S603 - the repository's own release script against a local origin and fakes
        [str(VERIFY_RELEASE), version], cwd=clone, env=env, capture_output=True, text=True, timeout=120, check=False
    )


def test_verify_release_checks_every_published_artifact(published: Path) -> None:
    result = verify_release(published)
    assert result.returncode == 0, result.stderr
    assert f"v{__version__} at {git(published, 'rev-parse', 'HEAD')}" in result.stdout
    assert "initialized, validated, searched and read a brain" in result.stdout
    assert not list((published.parent / "cache").iterdir())  # the temporary folder, installed tool included, is gone


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        ({"FAKE_DRAFT": "true"}, "is a draft"),
        ({"FAKE_ASSETS": WHEEL}, f"holds '{WHEEL}'"),
        ({"FAKE_PYPI_MISSING": "1"}, "PyPI does not serve"),
        ({"FAKE_PYPI": pypi(sdist="tampered")}, f"{SDIST} on GitHub does not match PyPI"),
        ({"FAKE_ATTESTATION": "1"}, "has no valid build attestation"),
        ({"FAKE_UV_FAIL": "1"}, f"could not install the verified {WHEEL}"),
    ],
)
def test_verify_release_fails_on_any_mismatch(published: Path, fake: dict[str, str], message: str) -> None:
    result = verify_release(published, **fake)
    assert (result.returncode, message in result.stderr) == (1, True), result.stderr


def test_verify_release_needs_a_valid_version_and_its_tag(published: Path) -> None:
    assert verify_release(published, "1.2").returncode == 2
    missing = verify_release(published, "9.9.9")
    assert (missing.returncode, "no annotated v9.9.9 on origin" in missing.stderr) == (1, True), missing.stderr


VERIFY_RELEASE_TAG = ROOT / "scripts" / "verify-release-tag.sh"
# A fake GitHub API: each `gh api PATH --jq EXPR` prints the tab-separated answer its variable holds.
TAG_GH = """#!/bin/sh
case "$2" in
  */git/ref/tags/*) printf '%s\\n' "$FAKE_REF" ;;
  */git/tags/*) printf '%s\\n' "$FAKE_TAG" ;;
  */git/ref/heads/main) printf '%s\\n' "$FAKE_MAIN" ;;
  */compare/*) printf '%s\\n' "$FAKE_COMPARE" ;;
  *) echo "unexpected gh $*" >&2; exit 64 ;;
esac
"""
COMMIT, OTHER = "a" * 40, "b" * 40


def verify_release_tag(tmp_path: Path, **fake: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "gh").write_text(TAG_GH)
    (tmp_path / "gh").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REF_NAME": "v1.2.3",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SHA": COMMIT,
        "FAKE_REF": f"commit\t{COMMIT}",
        "FAKE_MAIN": f"commit\t{COMMIT}",
        **fake,
    }
    return subprocess.run(  # noqa: S603 - the repository's own release guard against a fake API
        [str(VERIFY_RELEASE_TAG)], env=env, capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.parametrize(
    "fake",
    [
        {},
        # An annotated tag resolves through its tag object.
        {"FAKE_REF": f"tag\t{OTHER}", "FAKE_TAG": f"commit\t{COMMIT}"},
        # A later main still contains the release commit, so a rerun after publication keeps its authority.
        {"FAKE_MAIN": f"commit\t{OTHER}", "FAKE_COMPARE": "ahead"},
    ],
    ids=["lightweight", "annotated", "main-advanced"],
)
def test_verify_release_tag_accepts_the_workflow_commit_on_main(tmp_path: Path, fake: dict[str, str]) -> None:
    result = verify_release_tag(tmp_path, **fake)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        ({"FAKE_REF": f"commit\t{OTHER}"}, f"does not resolve to workflow commit {COMMIT}"),
        ({"FAKE_REF": f"tag\t{OTHER}", "FAKE_TAG": f"tree\t{COMMIT}"}, "unsupported object type tree"),
        # A tag on an unmerged branch never publishes.
        ({"FAKE_MAIN": f"commit\t{OTHER}", "FAKE_COMPARE": "diverged"}, "is not contained in current main"),
    ],
)
def test_verify_release_tag_refuses_any_other_commit(tmp_path: Path, fake: dict[str, str], message: str) -> None:
    result = verify_release_tag(tmp_path, **fake)
    assert (result.returncode, message in result.stderr) == (1, True), result.stderr
