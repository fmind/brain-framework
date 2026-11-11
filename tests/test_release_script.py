"""scripts/release.py ships only the named paths and syncs the primary checkout without touching other sessions' work."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "scripts" / "release.py"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.test",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.test",
}
# A fake uv whose `uv version --quiet X` rewrites pyproject.toml and requires mise trust, as a mise shim does.
UV = """#!/bin/sh
case ":$MISE_TRUSTED_CONFIG_PATHS:" in
  *":$(pwd):"*) ;;
  *) echo "mise.toml in $(pwd) is not trusted" >&2; exit 1 ;;
esac
[ "$1 $2" = "version --quiet" ] || { echo "unexpected uv $*" >&2; exit 64; }
sed -i.bak "s/^version = .*/version = \\"$3\\"/" pyproject.toml && rm pyproject.toml.bak
"""
MISE = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_MISE_CALLS"
"""
CHANGELOG = (
    "# Changelog\n\n## Unreleased\n\n### Added\n\n- **A change.** It works.\n\n## [v1.2.3](x) - 2026-01-01\n\nOld.\n"
)


def git(folder: Path, *args: str) -> str:
    env = {**os.environ, **IDENTITY}
    return subprocess.run(  # noqa: S603 - real Git on disposable repositories
        ["git", "-C", str(folder), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=env,
    ).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    """A primary checkout on main at version 1.2.3, level with its local origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)  # noqa: S603, S607
    checkout = tmp_path / "primary"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True, capture_output=True)  # noqa: S603, S607
    git(checkout, "checkout", "-q", "-B", "main")
    write(checkout / "pyproject.toml", '[project]\nname = "demo"\nversion = "1.2.3"\n')
    write(checkout / "src/bf/__init__.py", '__version__ = "1.2.3"\n')
    write(checkout / "src/bf/skills/demo/SKILL.md", "---\ncompatibility: Requires Brain Framework 1 (bf).\n---\n")
    write(checkout / "docs/demo.schema.json", "{}\n")
    write(checkout / "tests/contract/demo.schema.json", "{}\n")
    write(checkout / "README.md", "Needs Brain Framework 1.\n")
    write(checkout / "CHANGELOG.md", CHANGELOG)
    write(checkout / "src/a.txt", "a\n")
    write(checkout / "src/b.txt", "b\n")
    (checkout / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/release-notes.sh", checkout / "scripts/release-notes.sh")
    git(checkout, "add", "--all")
    git(checkout, "commit", "-q", "-m", "init")
    git(checkout, "push", "-q", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("uv", UV), ("mise", MISE)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    return checkout


def release(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        **IDENTITY,
        "PATH": f"{cwd.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "XDG_CACHE_HOME": str(cwd.parent / "cache"),
        "FAKE_MISE_CALLS": str(cwd.parent / "mise.calls"),
    }
    return subprocess.run(  # noqa: S603 - the repository's own release script against local repositories
        [sys.executable, str(RELEASE), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False
    )


def prepare(primary: Path, *args: str) -> tuple[Path, dict[str, object]]:
    result = release(primary, "prepare", *args)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    return Path(str(data["clone"])), data


def publish_locally(clone: Path, version: str) -> str:
    """What `publish` leaves behind, without GitHub: the commit on origin's main and its tag."""
    git(clone, "commit", "-q", "-m", f"chore(release): release v{version}")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "tag", "-a", f"v{version}", "-m", f"v{version}")
    return git(clone, "rev-parse", "HEAD")


def test_prepare_ships_only_the_named_paths(primary: Path) -> None:
    write(primary / "src/a.txt", "released\n")
    write(primary / "src/b.txt", "another session\n")
    write(primary / "src/new/c.txt", "added\n")
    clone, data = prepare(primary, "1.3.0", "src/a.txt", "src/new")
    assert data["files"] == ["src/a.txt", "src/new/c.txt"]
    assert data["major"] is False
    assert (clone / "src/a.txt").read_text() == "released\n"
    assert (clone / "src/new/c.txt").read_text() == "added\n"
    assert (clone / "src/b.txt").read_text() == "b\n"
    assert 'version = "1.3.0"' in (clone / "pyproject.toml").read_text()
    assert (clone / "src/bf/__init__.py").read_text() == '__version__ = "1.3.0"\n'
    changelog = (clone / "CHANGELOG.md").read_text()
    today = datetime.date.today().isoformat()
    assert (
        f"## Unreleased\n\n## [v1.3.0](https://github.com/fmind/brain-framework/releases/tag/v1.3.0) - {today}\n\n### Added"
        in changelog
    )
    assert "Requires Brain Framework 1 " in (clone / "src/bf/skills/demo/SKILL.md").read_text()
    # Everything is staged for review, and the primary checkout is untouched.
    assert git(clone, "status", "--porcelain", "--untracked-files=all").splitlines() == [
        "M  CHANGELOG.md",
        "M  pyproject.toml",
        "M  src/a.txt",
        "M  src/bf/__init__.py",
        "A  src/new/c.txt",
    ]
    assert (primary / "src/b.txt").read_text() == "another session\n"
    assert git(clone, "remote", "get-url", "origin") == git(primary, "remote", "get-url", "origin")


def test_prepare_a_major_updates_compatibility_and_the_contract(primary: Path) -> None:
    write(primary / "docs/demo.schema.json", '{"major": true}\n')
    clone, data = prepare(primary, "2.0.0", "--all")
    assert data["major"] is True
    assert "Requires Brain Framework 2 " in (clone / "src/bf/skills/demo/SKILL.md").read_text()
    assert (clone / "tests/contract/demo.schema.json").read_text() == '{"major": true}\n'
    assert (primary.parent / "mise.calls").read_text() == "run generate:schema\n"
    assert data["review"] == ["README.md:1:Needs Brain Framework 1."]


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (("1.3.0",), 2, "name the paths"),
        (("1.3.0", "src/a.txt", "--all"), 2, "name the paths"),
        (("1.3", "--all"), 2, "invalid input: expected a version"),
        (("1.3.0", "src/b.txt"), 1, "src/b.txt holds no change"),
        (("1.2.3", "--all"), 1, "not newer"),
    ],
)
def test_prepare_refuses_invalid_requests(primary: Path, args: tuple[str, ...], code: int, message: str) -> None:
    write(primary / "src/a.txt", "released\n")
    result = release(primary, "prepare", *args)
    assert result.returncode == code
    assert message in result.stderr
    assert not (primary.parent / "cache").exists()


def test_prepare_refuses_an_existing_tag_and_an_empty_changelog(primary: Path) -> None:
    git(primary, "tag", "v1.3.0")
    result = release(primary, "prepare", "1.3.0", "--all")
    assert (result.returncode, "already exists locally" in result.stderr) == (1, True)
    git(primary, "tag", "-d", "v1.3.0")
    write(primary / "CHANGELOG.md", CHANGELOG.replace("### Added\n\n- **A change.** It works.\n\n", ""))
    result = release(primary, "prepare", "1.3.0", "CHANGELOG.md")
    assert (result.returncode, "Unreleased section is empty" in result.stderr) == (1, True)
    # A failed prepare removes the clone it created, so a rerun starts clean.
    assert not (primary.parent / "cache/bf-release/v1.3.0").exists()


def test_sync_moves_the_primary_to_the_release_and_keeps_other_work(primary: Path) -> None:
    write(primary / "src/a.txt", "released\n")
    write(primary / "src/new.txt", "added\n")
    write(primary / "src/b.txt", "another session\n")
    clone, _ = prepare(primary, "1.3.0", "src/a.txt", "src/new.txt")
    released = publish_locally(clone, "1.3.0")
    result = release(clone, "sync")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["restored"] == ["src/a.txt", "src/new.txt"]
    assert git(primary, "rev-parse", "HEAD") == released
    assert (primary / "src/a.txt").read_text() == "released\n"
    assert 'version = "1.3.0"' in (primary / "pyproject.toml").read_text()
    assert git(primary, "status", "--porcelain") == "M src/b.txt"
    assert (primary / "src/b.txt").read_text() == "another session\n"


def test_sync_leaves_a_released_path_another_session_changed(primary: Path) -> None:
    write(primary / "src/a.txt", "released\n")
    clone, _ = prepare(primary, "1.3.0", "src/a.txt")
    publish_locally(clone, "1.3.0")
    write(primary / "src/a.txt", "edited after prepare\n")
    base = git(primary, "rev-parse", "HEAD")
    result = release(clone, "sync")
    assert result.returncode == 1
    assert "another session changed src/a.txt" in result.stderr
    assert git(primary, "rev-parse", "HEAD") == base
    assert (primary / "src/a.txt").read_text() == "edited after prepare\n"


def test_sync_leaves_another_sessions_staged_selection(primary: Path) -> None:
    write(primary / "src/a.txt", "partly staged\n")
    git(primary, "add", "src/a.txt")
    write(primary / "src/a.txt", "released\n")
    clone, _ = prepare(primary, "1.3.0", "src/a.txt")
    publish_locally(clone, "1.3.0")
    result = release(clone, "sync")
    assert result.returncode == 1
    assert "another session changed src/a.txt" in result.stderr


def test_publish_and_sync_need_a_committed_release(primary: Path) -> None:
    write(primary / "src/a.txt", "released\n")
    clone, _ = prepare(primary, "1.3.0", "src/a.txt")
    result = release(clone, "publish", "1.3.0")
    assert (result.returncode, "nothing is committed yet" in result.stderr) == (1, True)
    result = release(clone, "publish", "1.4.0")
    assert (result.returncode, "prepares 1.3.0, not 1.4.0" in result.stderr) == (1, True)
    result = release(clone, "sync")
    assert (result.returncode, "is not tagged in this clone" in result.stderr) == (1, True)
    result = release(primary, "check")
    assert (result.returncode, "not a release clone" in result.stderr) == (1, True)


def test_prepare_lists_outgoing_binaries(primary: Path) -> None:
    (primary / "src/logo.bin").write_bytes(b"\0\1binary")
    git(primary, "add", "src/logo.bin")
    git(primary, "commit", "-q", "-m", "add a binary")
    commit = git(primary, "rev-parse", "--short", "HEAD")
    git(primary, "rm", "-q", "src/logo.bin")
    git(primary, "commit", "-q", "-m", "delete it again")
    (primary / "src/icon.bin").write_bytes(b"\0\2binary")
    _, data = prepare(primary, "1.3.0", "src/icon.bin")
    assert data["binaries"] == [f"{commit}:src/logo.bin", "staged:src/icon.bin"]


def test_publish_refuses_a_tag_naming_another_commit(primary: Path) -> None:
    write(primary / "src/a.txt", "released\n")
    clone, _ = prepare(primary, "1.3.0", "src/a.txt")
    git(primary, "tag", "-a", "v1.3.0", "-m", "elsewhere")
    git(primary, "push", "-q", "origin", "v1.3.0")
    git(clone, "commit", "-q", "-m", "chore(release): release v1.3.0")
    result = release(clone, "publish", "1.3.0")
    assert result.returncode == 1
    assert "tags never move" in result.stderr


def test_publish_rerun_fetches_a_tag_only_origin_holds(primary: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(primary / "src/a.txt", "released\n")
    clone, _ = prepare(primary, "1.3.0", "src/a.txt")
    released = publish_locally(clone, "1.3.0")
    # push-and-tag deletes its local tag when the push reports a failure, though origin may have received it.
    git(clone, "push", "-q", "origin", "v1.3.0")
    git(clone, "tag", "-d", "v1.3.0")
    # A fake gh that lists no CD run, on the PATH `release` gives the clone.
    fake = clone.parent / "bin/gh"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("RELEASE_ATTEMPTS", "1")
    monkeypatch.setenv("RELEASE_DELAY", "0")
    result = release(clone, "publish", "1.3.0")
    assert (result.returncode, "no CD run was listed" in result.stderr) == (1, True), result.stderr
    # The rerun restored the tag `sync` needs instead of sending the release back to `publish`.
    assert git(clone, "rev-parse", "v1.3.0^{commit}") == released
