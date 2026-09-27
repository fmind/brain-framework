"""Exercise collaboration on fictional evidence in a disposable Git repository, without providers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from bf.models import Record
from bf.records import upsert
from bf.storage import Store, writer
from bf.validate import validate


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Git operations on a disposable fixture
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "-C", str(root), *args],  # noqa: S607
        text=True,
        capture_output=True,
        check=check,
        timeout=30,
    )


def save(root: Path, record_id: str, title: str) -> None:
    store = Store(root)
    with writer(store):
        upsert(store, "issues", [Record(id=record_id, title=title)], snapshot=False)


def commit(root: Path) -> None:
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Synthetic contribution")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bf-team-") as temporary:
        base = Path(temporary)
        os.environ["XDG_STATE_HOME"] = str(base / "state")
        os.environ["XDG_CONFIG_HOME"] = str(base / "config")
        for key in tuple(os.environ):
            if key.startswith("GIT_"):
                del os.environ[key]
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        root = base / "brain"
        root.mkdir()
        git(root, "init", "--quiet", "--initial-branch=main")
        git(root, "config", "user.name", "Example")
        git(root, "config", "user.email", "example@example.invalid")
        store = Store(root)
        store.write("bf.yaml", b"version: 6\nname: team\n")
        store.write(".gitignore", b"/.bf/\n")
        save(root, "shared", "Original decision")
        commit(root)
        for person in ("alice", "bob"):
            git(root, "checkout", "--quiet", "-b", person, "main")
            save(root, person, f"Independent evidence from {person}")
            store.write(
                f"actions/2026-09-27_review-{uuid4().hex}/ACTION.md",
                f"---\ntype: action\nstatus: draft\n---\n# Review\n\n{person}'s independent session.\n".encode(),
            )
            commit(root)
        independent = git(root, "merge", "--no-edit", "alice", check=False)
        report = validate(store)
        if independent.returncode or not report["valid"] or report["records"] != 3 or report["notes"] != 2:
            raise RuntimeError("independent contributions did not merge and validate")
        git(root, "branch", "combined")
        for person in ("carol", "dana"):
            git(root, "checkout", "--quiet", "-b", person, "combined")
            save(root, "shared", f"Incompatible decision from {person}")
            commit(root)
        competing = git(root, "merge", "--no-edit", "carol", check=False)
        rejected = not validate(store)["valid"]
        if competing.returncode != 1 or not rejected:
            raise RuntimeError("competing revisions were silently accepted")
        sys.stdout.write(
            json.dumps(
                {
                    "independent_merge": "clean",
                    "records": report["records"],
                    "actions": report["notes"],
                    "same_record_merge": "conflict",
                    "conflict_rejected": rejected,
                }
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
