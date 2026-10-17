"""The release scripts guard irreversible steps: a tag exists only after CI succeeded on its exact commit."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bf import __version__

ROOT = Path(__file__).resolve().parents[1]
PUSH_AND_TAG = ROOT / "scripts" / "push-and-tag.sh"
RELEASE_NOTES = ROOT / "scripts" / "release-notes.sh"
VERIFY_RELEASE = ROOT / "scripts" / "verify-release.sh"
# A fake gh: CI lists run 42 unless FAKE_RUN is empty, watching it exits with FAKE_WATCH and it concludes with
# FAKE_CONCLUSION. They differ when the watch succeeds on a run that ended without success, such as a skipped one.
# Each call's arguments are appended to FAKE_CALLS.
GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_CALLS"
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
        "FAKE_CALLS": str(clone.parent / "gh.calls"),
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


def gh_calls(clone: Path) -> list[str]:
    calls = clone.parent / "gh.calls"
    return calls.read_text().splitlines() if calls.exists() else []


def reject(clone: Path, refs: str) -> Path:
    """Make origin refuse pushes to the refs matching a shell pattern, as a network drop or a ruleset would."""
    hook = clone.parent / "origin.git" / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)  # Git copies hooks/ from its templates, which a minimal installation lacks.
    hook.write_text(f'#!/bin/sh\nwhile read -r _ _ ref; do case "$ref" in {refs}) exit 1 ;; esac; done\n')
    hook.chmod(0o755)
    return hook


def test_push_and_tag_tags_the_pushed_commit_after_ci_succeeds(release: Path) -> None:
    result = push_and_tag(release)
    assert result.returncode == 0, result.stderr
    head = git(release, "rev-parse", "HEAD")
    assert git(release, "rev-parse", "origin/main") == head
    assert git(release, "rev-parse", "v1.2.3^{commit}") == head
    assert "refs/tags/v1.2.3" in remote_tags(release)
    # It watches the CI run of the pushed commit, not the newest run on main, which may test another commit.
    listed = f"run list --workflow ci.yml --commit {head} --event push --branch main"
    assert gh_calls(release) == [
        f"{listed} --json databaseId -q .[0].databaseId // empty",
        "run watch 42 --exit-status",
        "run view 42 --json conclusion -q .conclusion",
    ]
    # A tag never moves: a second run refuses before pushing anything, whether the tag is local or only on origin.
    again = push_and_tag(release)
    assert (again.returncode, "v1.2.3 already exists locally" in again.stderr) == (1, True), again.stderr
    git(release, "tag", "-d", "v1.2.3")
    remote = push_and_tag(release)
    assert (remote.returncode, "v1.2.3 already exists on origin" in remote.stderr) == (1, True), remote.stderr
    assert not git(release, "tag", "--list")
    assert len(gh_calls(release)) == 3  # Neither refusal asked CI anything.


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


def test_a_failed_main_push_tags_nothing_and_asks_ci_nothing(release: Path) -> None:
    reject(release, "refs/heads/*")
    failed = push_and_tag(release)
    assert (failed.returncode, "pushing main failed; nothing tagged" in failed.stderr) == (1, True), failed.stderr
    assert not git(release, "tag", "--list")
    assert not git(release, "ls-remote", "origin")
    assert not gh_calls(release)


def test_a_failed_tag_push_deletes_the_local_tag_so_a_rerun_retries(release: Path) -> None:
    # Origin rejects tags after accepting main.
    hook = reject(release, "refs/tags/*")
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

    def notes(sections: str, tag: str = "v1.2.3") -> subprocess.CompletedProcess[str]:
        changelog.write_text(f"# Changelog\n\n{sections}")
        return subprocess.run(  # noqa: S603 - the repository's own release script
            [str(RELEASE_NOTES), tag, str(changelog)], capture_output=True, text=True, timeout=30, check=False
        )

    released = "## Unreleased\n\n## [v1.2.3](url) - 2026-09-29\n\nFixed.\n\n## [v1.2.2](url)\n\nOld.\n"
    assert notes(released).stdout == "\nFixed.\n\n"
    # Any level-2 heading ends the section, so a draft placed below it never reaches the release.
    assert notes("## [v1.2.3](url)\n\nFixed.\n\n## Unreleased\n\n- Draft entry.\n").stdout == "\nFixed.\n\n"
    for sections, tag, message in (
        (released, "v9.9.9", "no section for v9.9.9"),
        ("## [v1.2.3](url)\n\nFixed.\n\n## [v1.2.3](url)\n\nAgain.\n", "v1.2.3", "duplicate sections for v1.2.3"),
        ("## [v1.2.3](url)\n\n## [v1.2.2](url)\n\nOld.\n", "v1.2.3", "empty section for v1.2.3"),
    ):
        refused = notes(sections, tag)
        assert (refused.returncode, refused.stdout, message in refused.stderr) == (1, "", True), refused.stderr


def test_release_checklist_reads_the_workflows_mise_pin(tmp_path: Path) -> None:
    # The checklist downloads the mise release the workflows pin: an inline comment after the pin once left the
    # version empty, and the download URL silently pointed at no release.
    lines = (ROOT / ".agents/skills/bf-contribute/references/release.md").read_text().splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("version=$("))
    script = "\n".join([*lines[start : start + 2], 'printf %s "$version"'])
    workflow = (ROOT / ".github/workflows/verify.yml").read_text()
    pinned = next(line.split()[1] for line in workflow.splitlines() if line.strip().startswith("version: 20"))
    # Every workflow pins that same mise, so the scheduled security jobs parse the tasks the checklist checked.
    pins = {
        line.split()[1]
        for path in (ROOT / ".github/workflows").glob("*.yml")
        for line in path.read_text().splitlines()
        if line.strip().startswith("version: 20")
    }
    assert pins == {pinned}

    def read(root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the checklist's own commands
            ["bash", "--noprofile", "--norc", "-euc", script],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    found = read(ROOT)
    assert (found.returncode, found.stdout) == (0, pinned)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/verify.yml").write_text("      version: latest\n")
    missing = read(tmp_path)
    assert missing.returncode != 0
    assert "no mise version pin" in missing.stderr


def test_workflow_pipelines_fail_when_any_command_fails() -> None:
    # GitHub runs a step without `shell:` as `bash -e`, which ignores a failure before a pipeline's last command: jq
    # rejecting PyPI's metadata would surface only as a hash mismatch. `shell: bash` adds pipefail.
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            for step in job.get("steps", []):
                # shlex reads bash's `|&` as one token.
                if {"|", "|&"} & set(shlex.shlex(step.get("run", ""), posix=True, punctuation_chars=True)):
                    assert step.get("shell") == "bash", f"{path.name}: {step['name']}"


WHEEL = f"brain_framework-{__version__}-py3-none-any.whl"
SDIST = f"brain_framework-{__version__}.tar.gz"
# Fakes for verify-release: gh describes the release from FAKE_* variables, downloads two small assets and checks an
# attestation only with the FAKE_POLICY flags; curl prints FAKE_PYPI unless FAKE_PYPI_MISSING is set; and uv installs
# the downloaded wheel, unless FAKE_UV_FAIL is set, as a bf that runs this checkout or only reports FAKE_BF_VERSION.
VERIFY_GH = """#!/bin/sh
case "$1 $2" in
  "release view")
    printf '%s\\t%s\\t%s\\t%s\\n' "${FAKE_DRAFT-false}" "${FAKE_PRERELEASE-false}" https://example.test/release \\
      "$FAKE_ASSETS" ;;
  "release download")
    while [ "$#" -gt 0 ]; do [ "$1" = --dir ] && dir=$2; shift; done
    mkdir -p "$dir" && printf wheel > "$dir/$FAKE_WHEEL" && printf sdist > "$dir/$FAKE_SDIST" ;;
  "attestation verify")
    shift 3
    [ "$*" = "$FAKE_POLICY" ] || { echo "unexpected attestation policy $*" >&2; exit 64; }
    exit "${FAKE_ATTESTATION-0}" ;;
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
[ -z "${FAKE_BF_VERSION-}" ] || printf '#!/bin/sh\\necho %s\\n' "$FAKE_BF_VERSION" > "$UV_TOOL_BIN_DIR/bf"
chmod +x "$UV_TOOL_BIN_DIR/bf"
"""


def pypi(wheel: str = "wheel", sdist: str = "sdist", *, also: str = "") -> str:
    """PyPI's JSON for the release, with the digests of the given asset contents, and file `also` when named."""
    files = [(WHEEL, wheel), (SDIST, sdist), *([(also, "")] if also else [])]
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
    commit = git(clone, "rev-parse", "HEAD")
    env = {
        **os.environ,
        "PATH": f"{clone.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "XDG_CACHE_HOME": str(clone.parent / "cache"),
        "VERIFY_RELEASE_REPOSITORY": "owner/repo",
        # The only attestation the fake gh accepts: one signed by CD's own run for the tag and its commit.
        "FAKE_POLICY": f"--repo owner/repo --source-ref refs/tags/v{version} --source-digest {commit} "
        "--signer-workflow owner/repo/.github/workflows/cd.yml",
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


def test_verify_release_ignores_a_relative_cache_home(published: Path) -> None:
    home = published.parent / "home"
    result = verify_release(published, HOME=str(home), XDG_CACHE_HOME="cache")
    assert result.returncode == 0, result.stderr
    # As bf does, it falls back to ~/.cache: relative settings would let the installed bf use the caller's own.
    assert not (published / "cache").exists()
    assert [path.name for path in home.iterdir()] == [".cache"]
    assert not list((home / ".cache").iterdir())


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        ({"FAKE_DRAFT": "true"}, "is a draft"),
        ({"FAKE_PRERELEASE": "true"}, "is a prerelease"),
        ({"FAKE_ASSETS": WHEEL}, f"holds '{WHEEL}'"),
        ({"FAKE_PYPI_MISSING": "1"}, "PyPI does not serve"),
        # A platform wheel that CD never builds.
        (
            {"FAKE_PYPI": pypi(also=f"brain_framework-{__version__}-cp314-cp314-manylinux_2_28_x86_64.whl")},
            "PyPI lists 3 files",
        ),
        ({"FAKE_PYPI": pypi(sdist="tampered")}, f"{SDIST} on GitHub does not match PyPI"),
        ({"FAKE_ATTESTATION": "1"}, "has no valid build attestation"),
        ({"FAKE_UV_FAIL": "1"}, f"could not install the verified {WHEEL}"),
        ({"FAKE_BF_VERSION": "0.0.0"}, f"the installed bf does not report {__version__}"),
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
