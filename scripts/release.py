"""Prepare, check, publish and sync a Brain Framework release from a clean clone.

Several sessions may edit the primary checkout at once, so a release ships exactly the paths it names:

    python3 scripts/release.py prepare X.Y.Z PATH...  # in the primary checkout, or --all for every change
    python3 scripts/release.py check                  # in the clone: the full gate as CI runs it
    python3 scripts/release.py publish X.Y.Z          # in the clone, after committing: push, tag, CD, verify, sync
    python3 scripts/release.py sync                   # in the clone: rerun the primary checkout's sync alone

`prepare` clones HEAD into the cache, copies the named working-tree paths, bumps the version, dates the Unreleased
changelog section and, for a new major, updates the skills' compatibility and refreshes the contract. `sync` moves the
primary checkout to the released commit only when each released path still holds its base, copied or released content:
anything else belongs to another session and stops the sync with nothing changed. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MANIFEST = "bf-release.json"  # Inside the clone's .git folder, so it never reaches a commit.
REPOSITORY = "https://github.com/fmind/brain-framework"
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
COMPATIBILITY = re.compile(r"^(compatibility: Requires Brain Framework )(\d+)\b", re.MULTILINE)


class ReleaseError(Exception):
    """A failed precondition or command; the message names the recovery."""


def run(*argv: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True, strip: bool = True) -> str:
    """Run a command without a shell and return its stdout, stripped unless asked; a failure raises with stderr."""
    result = subprocess.run(  # noqa: S603 - fixed release tools with argument lists, never a shell
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleaseError(f"`{' '.join(argv[:3])}` failed in {cwd}: {detail}")
    return result.stdout.strip() if strip else result.stdout


def stream(*argv: str, cwd: Path, env: dict[str, str] | None = None) -> None:
    """Run a long command with its output visible, as the gate, CI and CD waits are."""
    if subprocess.run(argv, cwd=cwd, env=env, check=False).returncode != 0:  # noqa: S603 - fixed release tools
        raise ReleaseError(f"`{' '.join(argv)}` failed in {cwd}; fix the cause and rerun this step")


def reply(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")


def parse_version(text: str) -> tuple[int, int, int]:
    match = VERSION.match(text)
    if not match:
        raise ValueError(f"expected a version such as 20.1.0, not {text}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def digest(path: Path) -> str | None:
    """The SHA-256 of a regular file, None when absent; a link or folder never matches a released file."""
    if path.is_symlink() or path.is_dir():
        return "not-a-regular-file"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def blob(repository: Path, commit: str, path: str) -> str | None:
    """The SHA-256 of PATH at COMMIT, None when the commit does not hold it."""
    result = subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", "-C", str(repository), "cat-file", "blob", f"{commit}:{path}"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None


def cache_root() -> Path:
    # As bf does, ignore a relative XDG_CACHE_HOME. Keep the clone on disk: the gate's environments are large.
    configured = os.environ.get("XDG_CACHE_HOME", "")
    return Path(configured) if configured.startswith("/") else Path.home() / ".cache"


def toplevel() -> Path:
    return Path(run("git", "rev-parse", "--show-toplevel", cwd=Path.cwd()))


def trusted_env(clone: Path, **extra: str) -> dict[str, str]:
    """Trust the disposable clone's mise.toml when tools such as uv or gh run through mise shims."""
    existing = os.environ.get("MISE_TRUSTED_CONFIG_PATHS", "")
    paths = f"{clone}{os.pathsep}{existing}" if existing else str(clone)
    return {**os.environ, "MISE_TRUSTED_CONFIG_PATHS": paths, **extra}


def load_manifest(clone: Path) -> dict[str, Any]:
    path = clone / ".git" / MANIFEST
    if not path.is_file():
        raise ReleaseError(f"{clone} is not a release clone: run `prepare` in the primary checkout first")
    return json.loads(path.read_text())


def changed_paths(root: Path) -> list[str]:
    """Every tracked change and untracked, unignored file, renames split into a deletion and an addition."""
    output = run(
        "git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", cwd=root, strip=False
    )
    return sorted({entry[3:] for entry in output.split("\0") if entry})


def select(changed: list[str], pathspecs: list[str]) -> list[str]:
    selected: set[str] = set()
    for spec in pathspecs:
        prefix = spec.rstrip("/") + "/"
        matches = [path for path in changed if path == spec.rstrip("/") or path.startswith(prefix)]
        if not matches:
            raise ReleaseError(f"{spec} holds no change in the primary checkout; name only changed paths")
        selected.update(matches)
    return sorted(selected)


def prepare(version: str, pathspecs: list[str], *, every: bool) -> None:
    new = parse_version(version)
    tag = f"v{version}"
    root = toplevel()
    if (root / ".git" / MANIFEST).exists():
        raise ReleaseError("this is a release clone: run `prepare` in the primary checkout")
    if run("git", "branch", "--show-current", cwd=root) != "main":
        raise ReleaseError("release from main: switch the primary checkout to main first")
    run("git", "fetch", "--quiet", "origin", cwd=root)
    if subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", "-C", str(root), "merge-base", "--is-ancestor", "origin/main", "HEAD"],  # noqa: S607
        check=False,
    ).returncode:
        raise ReleaseError("HEAD does not contain origin/main: rebase your commits onto it first")
    if run("git", "rev-list", "--merges", "origin/main..HEAD", cwd=root):
        raise ReleaseError(
            "main rejects merge commits: stack or cherry-pick the outgoing commits into a linear history"
        )
    if run("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}", cwd=root, check=False):
        raise ReleaseError(f"{tag} already exists locally; tags never move, so choose the next version")
    if run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", cwd=root):
        raise ReleaseError(f"{tag} already exists on origin; tags never move, so choose the next version")
    pyproject = run("git", "show", "HEAD:pyproject.toml", cwd=root)
    declared = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    if not declared:
        raise ReleaseError("pyproject.toml at HEAD declares no version")
    current = parse_version(declared.group(1))
    if new <= current:
        raise ReleaseError(f"{version} is not newer than the current {declared.group(1)}")

    files = select(changed_paths(root), pathspecs) if pathspecs else changed_paths(root) if every else []
    clone = cache_root() / "bf-release" / tag
    if clone.exists():
        raise ReleaseError(f"{clone} exists: finish that release or remove the folder")
    base = run("git", "rev-parse", "HEAD", cwd=root)
    origin = run("git", "remote", "get-url", "origin", cwd=root)
    clone.parent.mkdir(parents=True, exist_ok=True)
    try:
        run("git", "clone", "--quiet", "--no-local", "--no-checkout", str(root), str(clone), cwd=root)
        run("git", "checkout", "--quiet", "-B", "main", base, cwd=clone)
        run("git", "remote", "set-url", "origin", origin, cwd=clone)
        run("git", "fetch", "--quiet", "origin", cwd=clone)
        run("git", "branch", "--quiet", "--set-upstream-to", "origin/main", cwd=clone)
        copied = copy(root, clone, files)
        review = bump(clone, version, current[0], new[0], is_major=new[0] > current[0])
        run("git", "add", "--all", cwd=clone)
        binaries = outgoing_binaries(clone)
        (clone / ".git" / MANIFEST).write_text(
            json.dumps({"version": version, "primary": str(root), "base": base, "files": copied}, indent=2) + "\n"
        )
    except BaseException:
        shutil.rmtree(clone, ignore_errors=True)  # Only this run created it; a rerun starts clean.
        raise
    reply(
        {
            "clone": str(clone),
            "base": base,
            "files": files,
            "major": new[0] > current[0],
            "review": review,
            "binaries": binaries,
            "next": "write the release paragraph, review `git diff --cached -M`, run `check`, commit, then `publish`",
        }
    )


def outgoing_binaries(clone: Path) -> list[str]:
    """Binaries added or modified by outgoing commits, even if deleted later, and staged: Git diffs only name them."""
    found: list[str] = []
    commit = ""
    log = run("git", "log", "--diff-filter=AM", "--numstat", "--format=commit %h", "origin/main..HEAD", cwd=clone)
    staged = run("git", "diff", "--cached", "--diff-filter=AM", "--numstat", cwd=clone)
    for line in [*log.splitlines(), "commit staged", *staged.splitlines()]:
        if line.startswith("commit "):
            commit = line.removeprefix("commit ")
        elif line.startswith("-\t-\t"):
            found.append(f"{commit}:{line[4:]}")
    return found


def copy(root: Path, clone: Path, files: list[str]) -> dict[str, str | None]:
    """Copy each released path's working-tree content, or its deletion, and record what was copied."""
    copied: dict[str, str | None] = {}
    for name in files:
        source, target = root / name, clone / name
        if source.is_symlink() or (source.exists() and not source.is_file()):
            raise ReleaseError(f"{name} is not a regular file: commit it in the primary checkout instead")
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif blob(clone, "HEAD", name) is not None:
            run("git", "rm", "--quiet", "--", name, cwd=clone)
        copied[name] = digest(source)
    return copied


def bump(clone: Path, version: str, old_major: int, new_major: int, *, is_major: bool) -> list[str]:
    """Set the version, date the changelog section and, for a major, refresh compatibility and the contract."""
    tag, env = f"v{version}", trusted_env(clone)
    run("uv", "version", "--quiet", version, cwd=clone, env=env)
    init = clone / "src/bf/__init__.py"
    text, count = re.subn(
        r'^__version__ = "[^"]+"$', f'__version__ = "{version}"', init.read_text(), flags=re.MULTILINE
    )
    if count != 1:
        raise ReleaseError('src/bf/__init__.py must hold exactly one `__version__ = "..."` line')
    init.write_text(text)
    changelog = clone / "CHANGELOG.md"
    text = changelog.read_text()
    if text.count("\n## Unreleased\n") != 1:
        raise ReleaseError("CHANGELOG.md must hold exactly one `## Unreleased` heading")
    today = datetime.date.today().isoformat()
    heading = f"## [{tag}]({REPOSITORY}/releases/tag/{tag}) - {today}"
    changelog.write_text(text.replace("\n## Unreleased\n", f"\n## Unreleased\n\n{heading}\n", 1))
    try:
        run("scripts/release-notes.sh", tag, cwd=clone)
    except ReleaseError as error:
        # Name the usual cause; any other, such as a duplicate section, keeps the script's own message.
        if "empty section" not in str(error):
            raise
        raise ReleaseError(
            "the Unreleased section is empty: write its entries and name CHANGELOG.md in the paths"
        ) from error
    if not is_major:
        return []
    for skill in sorted(clone.glob("src/bf/skills/*/SKILL.md")):
        text, count = COMPATIBILITY.subn(rf"\g<1>{new_major}", skill.read_text())
        if count != 1:
            raise ReleaseError(f"{skill.relative_to(clone)} must hold one `compatibility: Requires Brain Framework N`")
        skill.write_text(text)
    run("mise", "run", "generate:schema", cwd=clone, env=env)
    for schema in sorted(clone.glob("docs/*.schema.json")):
        shutil.copy2(schema, clone / "tests/contract" / schema.name)
    # Mentions of the previous major that may mean the current release; historical ones, such as upgrade notes, stay.
    # Python's regex, not `git grep -E`: macOS's POSIX regex has no `\b`, so the search found nothing there.
    pattern = re.compile(rf"(Brain Framework|bf`?) {old_major}\b|\b{old_major}\.[0-9]+\.[0-9]+")
    listed = run(
        "git", "ls-files", "-z", "--", "src/bf/skills", "examples", "docs/docs", "README.md", cwd=clone, strip=False
    )
    found = []
    for name in sorted(filter(None, listed.split("\0"))):
        try:
            lines = (clone / name).read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeDecodeError:
            # Binary or unreadable files hold no prose to review.
            continue
        found.extend(f"{name}:{number}:{line}" for number, line in enumerate(lines, start=1) if pattern.search(line))
    return found


def check() -> None:
    clone = toplevel()
    load_manifest(clone)
    # GitHub Actions makes Typer color its errors, which once failed a tag's gate that had passed locally.
    env = trusted_env(clone, GITHUB_ACTIONS="true")
    stream("uv", "sync", "--locked", cwd=clone, env=env)
    stream("mise", "run", "all", cwd=clone, env=env)


def publish(version: str) -> None:
    parse_version(version)
    clone = toplevel()
    manifest = load_manifest(clone)
    if manifest["version"] != version:
        raise ReleaseError(f"this clone prepares {manifest['version']}, not {version}")
    if run("git", "rev-parse", "HEAD", cwd=clone) == manifest["base"]:
        raise ReleaseError("nothing is committed yet: commit the release with a Conventional Commit first")
    tag, env = f"v{version}", trusted_env(clone)
    tagged = run("git", "ls-remote", "origin", f"refs/tags/{tag}^{{}}", cwd=clone).split("\t")[0]
    if tagged and tagged != run("git", "rev-parse", "HEAD", cwd=clone):
        raise ReleaseError(f"{tag} on origin names {tagged}, not HEAD; tags never move, so investigate first")
    if tagged:
        # A rerun after push-and-tag reported a failure may lack the local tag that `sync` needs; a different local
        # tag fails the fetch instead of moving.
        run("git", "fetch", "--quiet", "origin", f"refs/tags/{tag}:refs/tags/{tag}", cwd=clone)
    else:
        # push-and-tag refuses a dirty tree, another branch or an existing tag, and tags only after CI succeeded.
        stream(str(clone / "scripts/push-and-tag.sh"), version, cwd=clone, env=env)
    attempts = int(os.environ.get("RELEASE_ATTEMPTS", "60"))
    delay = float(os.environ.get("RELEASE_DELAY", "5"))
    run_id = ""
    for _ in range(attempts):
        run_id = run(
            "gh", "run", "list", "--workflow", "cd.yml", "--branch", tag, "--limit", "1",
            "--json", "databaseId", "-q", ".[0].databaseId // empty", cwd=clone, env=env,
        )  # fmt: skip
        if run_id:
            break
        time.sleep(delay)
    if not run_id:
        raise ReleaseError(f"no CD run was listed for {tag}: check the tag's workflow, then rerun `publish`")
    stream("gh", "run", "watch", run_id, "--exit-status", cwd=clone, env=env)
    stream(str(clone / "scripts/verify-release.sh"), version, cwd=clone, env=env)
    sync()


def sync() -> None:
    clone = toplevel()
    manifest = load_manifest(clone)
    primary, base = Path(str(manifest["primary"])), str(manifest["base"])
    files: dict[str, str | None] = manifest["files"]
    released = run(
        "git", "rev-parse", "-q", "--verify", f"refs/tags/v{manifest['version']}^{{commit}}", cwd=clone, check=False
    )
    if not released:
        raise ReleaseError(f"v{manifest['version']} is not tagged in this clone: run `publish` first")
    if run("git", "branch", "--show-current", cwd=primary) != "main":
        raise ReleaseError(f"{primary} left main: sync it by hand")
    if run("git", "rev-parse", "HEAD", cwd=primary) != base:
        raise ReleaseError(f"{primary} moved from {base}: rebase its new commits onto origin/main by hand")
    run("git", "fetch", "--quiet", "origin", cwd=primary)
    if run("git", "rev-parse", "origin/main", cwd=primary) != released:
        raise ReleaseError("origin/main is not the released commit: integrate the primary checkout by hand")
    diff = run("git", "diff", "--name-only", "-z", "--no-renames", base, released, cwd=clone)
    paths = sorted({*filter(None, diff.split("\0")), *files})
    restore, others = [], []
    for name in paths:
        current = digest(primary / name)
        allowed = {blob(clone, base, name), blob(clone, released, name)}
        if name in files:
            allowed.add(files[name])
        # A staged version that is neither the base nor the working tree is another session's selection.
        staged = blob(primary, "", name)
        if current not in allowed or staged not in {blob(clone, base, name), current}:
            others.append(name)
        elif current != blob(clone, base, name):
            restore.append(name)
    if others:
        raise ReleaseError(f"another session changed {', '.join(others)}: nothing synced, integrate by hand")
    for name in restore:
        if blob(clone, base, name) is None:
            run("git", "rm", "--quiet", "--cached", "--ignore-unmatch", "--", name, cwd=primary)
            (primary / name).unlink()
        else:
            run("git", "checkout", "--quiet", base, "--", name, cwd=primary)
    run("git", "merge", "--quiet", "--ff-only", released, cwd=primary)
    reply({"primary": str(primary), "head": released, "restored": restore, "remove": str(clone)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    prepared = commands.add_parser("prepare", help="clone HEAD with the named paths and bump the version")
    prepared.add_argument("version")
    prepared.add_argument("paths", nargs="*", help="changed paths or folders the release ships")
    prepared.add_argument("--all", action="store_true", help="ship every change in the primary checkout")
    commands.add_parser("check", help="run the full gate in the release clone as CI does")
    published = commands.add_parser("publish", help="push, tag after CI, wait for CD, verify and sync")
    published.add_argument("version")
    commands.add_parser("sync", help="move the primary checkout to the released commit")
    arguments = parser.parse_args()
    try:
        if arguments.command == "prepare":
            if bool(arguments.paths) == arguments.all:
                parser.error("name the paths the release ships, or pass --all, but not both")
            prepare(arguments.version, arguments.paths, every=arguments.all)
        elif arguments.command == "check":
            check()
        elif arguments.command == "publish":
            publish(arguments.version)
        else:
            sync()
    except ValueError as error:
        sys.stderr.write(f"release: invalid input: {error}\n")
        return 2
    except ReleaseError as error:
        sys.stderr.write(f"release: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
