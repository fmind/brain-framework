# Release checklist

Use only for an explicitly authorized release. [CD](../../../../.github/workflows/cd.yml) owns PyPI and GitHub publication; never create a competing release, move a tag or replace published assets. Keep private-brain details out of outgoing commits, release notes and proposals. Most of a release is waiting on the gate, CI and CD: run them in the background.

1. **Prepare.** Inspect staged, unstaged and untracked work; confirm the release scope and reconcile `main` with the remote. Check that the proposed tag is absent locally and remotely. [Build the release worktree](#build-the-release-worktree) and work there from now on. [Inspect outgoing history](#inspect-outgoing-history), including binaries and files deleted by later commits.
1. **Version.** Run `uv version X.Y.Z` to update `pyproject.toml` and `uv.lock`. Match `src/bf/__init__.py`. For a new major, update each `src/bf/skills/*/SKILL.md` `compatibility`, which `tests/test_skills.py` checks, then search the skills, examples and docs for the previous major: update the mentions that mean the current release and keep historical ones, such as upgrade notes. Before 18.0.0, for example:

   ```bash
   git grep -nE '(Brain Framework|bf`?) 17\b|\b17\.[0-9]+\.[0-9]+' -- src/bf/skills examples docs/docs README.md
   ```

   Move `Unreleased` entries into `## [vX.Y.Z](https://github.com/fmind/brain-framework/releases/tag/vX.Y.Z) - YYYY-MM-DD`, including breaking changes and manual upgrade steps, leaving an empty `## Unreleased` above it. Open the section with one paragraph naming what stays unchanged and the upgrade step, such as `bf skills DIR` when a packaged skill changed: “A documentation release. The brain format (`version: 7`), `bf.yaml`, the reply schemas and the search cache are unchanged; no upgrade step is needed.” Confirm `scripts/release-notes.sh vX.Y.Z` returns only that section: `check:changelog` catches a missing, duplicate or empty section, not another heading, such as `## Unreleased`, placed below it.
1. **Contract.** A major release refreshes the contract after `mise run generate:schema`, with `cp docs/*.schema.json tests/contract/`, so `tests/test_contract.py` guards the new contract. A minor or patch release keeps `tests/contract/` unchanged, so the gate rejects [any incompatible schema change](../../../../CONTRIBUTING.md#keep-the-contract). A patch or documentation release skips this step and the previous-major search.
1. **Qualify.** Follow the [contribution verification procedure](../../../../CONTRIBUTING.md#set-up-and-check-a-change), including applicable generated files, then run the full gate as CI does, after `uv sync --locked`: `GITHUB_ACTIONS=true mise run all`. GitHub Actions makes Typer color its errors, which once failed a tag's gate that had passed locally. When task settings change, [check them with the pinned mise](#check-tasks-with-the-pinned-mise). Run `dot agent context --check` when available. Ask for an independent review of `git diff --cached -M`, such as a second agent with read-only instructions: it catches overclaims and stale references that tests miss. Inspect the final diff and qualify the exact release candidate; never weaken checks to publish.
1. **Publish.** Check that the primary checkout still matches the patch, then commit the authorized scope on `main` in the worktree with Conventional Commits and hooks enabled. [Push and tag](#push-and-tag) with `scripts/push-and-tag.sh X.Y.Z`: it pushes `main`, waits for CI on that commit across all four platforms, then tags that same commit. Wait for CD to rerun the shared gate and publish its tested artifacts to PyPI and GitHub without rebuilding them:

   ```bash
   gh run watch "$(gh run list --workflow cd.yml --branch vX.Y.Z --limit 1 --json databaseId -q '.[0].databaseId')" --exit-status
   ```

   Then [sync the primary checkout](#sync-the-primary-checkout).
1. **Verify.** Run `scripts/verify-release.sh X.Y.Z` from the checkout. It checks that origin holds the annotated tag, reporting its commit, then a non-draft GitHub release holding exactly the wheel and source archive, PyPI SHA-256 digests matching them and their build attestations, then installs that verified wheel in a temporary folder and runs version, initialization, validation, search and exact read. Installing the downloaded wheel sidesteps PyPI's index, which can refuse a version for minutes after its JSON API lists it; `tests/test_release.py` checks a draft release, the asset list, a missing PyPI version, digest and attestation mismatches and a failed install. Confirm CI's Pages deployment separately.
1. **Report.** Provide the release URL, version, commit, [verified boundaries and remaining limits](../../../../CONTRIBUTING.md#release). Release tests use synthetic data and fake providers; provider freshness and native host routing require separate evidence.

## Build the release worktree

Several sessions may edit the shared checkout, so qualify and commit in a detached worktree holding exactly the release scope. From the primary checkout, with the release content staged, unstaged or untracked there:

```bash
git worktree add --detach ~/.cache/bf-release HEAD
git diff HEAD --binary -M > ~/.cache/bf-release.patch
sha256sum ~/.cache/bf-release.patch
git -C ~/.cache/bf-release apply --index ~/.cache/bf-release.patch
```

Copy each untracked file that belongs to the release into the worktree and `git add` it, then compare `git -C ~/.cache/bf-release status --short` with the intended scope. Keep the worktree on disk, not in a RAM-backed `/tmp`: the gate's environments are large. Before committing, run `git diff HEAD --binary -M | sha256sum` in the primary checkout again: a different hash means another session changed it, so rebuild the patch. Commit with `git -C ~/.cache/bf-release checkout --ignore-other-worktrees main` followed by `git commit`; the agent's shell may reset its directory between commands, so pass `-C` or `cd` in the same command.

## Sync the primary checkout

Committing in the worktree moves `main` under the primary checkout, whose index and files still hold the old snapshot. There, `git diff HEAD --stat` must list only the edits made in the worktree after the patch, such as the version bump, and each untracked release file must match its commit, as `git show HEAD:PATH | cmp - PATH` shows. Then `git reset --hard HEAD` is safe; it keeps unrelated untracked files. Anything else belongs to another session: leave the checkout as it is and report it. Remove the worktree with `git worktree remove ~/.cache/bf-release`.

## Check tasks with the pinned mise

The workflows pin mise beside `jdx/mise-action` in `.github/workflows/verify.yml`. A newer global configuration can break that version, so parse the tasks with the pinned binary and an empty configuration (Linux x64 shown; pick the archive matching your platform):

```bash
version=$(sed -n 's/^ *version: \(20[0-9.]*\)$/\1/p' .github/workflows/verify.yml | head -n 1)
pinned=~/.cache/mise-$version
mkdir -p "$pinned/empty"
curl -fsSL "https://github.com/jdx/mise/releases/download/v$version/mise-v$version-linux-x64.tar.gz" | tar -xz -C "$pinned"
env MISE_CONFIG_DIR="$pinned/empty" MISE_GLOBAL_CONFIG_FILE="$pinned/empty/config.toml" MISE_DATA_DIR="$pinned/data" \
  MISE_CACHE_DIR="$pinned/cache" MISE_TRUSTED_CONFIG_PATHS="$PWD" "$pinned/mise/bin/mise" tasks info TASK --json
env MISE_CONFIG_DIR="$pinned/empty" MISE_GLOBAL_CONFIG_FILE="$pinned/empty/config.toml" MISE_DATA_DIR="$pinned/data" \
  MISE_CACHE_DIR="$pinned/cache" MISE_TRUSTED_CONFIG_PATHS="$PWD" "$pinned/mise/bin/mise" install --locked --dry-run
rm -rf "$pinned"
```

## Push and tag

From the clean release worktree on `main`, run the script file itself, never an inline copy of its commands. As its own process, its `set -euo pipefail` holds whatever shell calls it, so `cd ~/.cache/bf-release && scripts/push-and-tag.sh X.Y.Z` is safe, whereas `set -e` inside an inline `( … )` after `&&` is ignored and once let a failed CI run be tagged:

```bash
scripts/push-and-tag.sh X.Y.Z
```

It refuses a branch other than `main`, a dirty worktree, a version `pyproject.toml` does not declare and a tag that already exists locally or on origin, then pushes `main`, waits for the CI run of that exact commit and tags it only when the run succeeded. A failed push, a missing run or any other conclusion exits 1 with nothing tagged, and a failed tag push deletes the local tag again; `tests/test_release.py` checks each case.

## Inspect outgoing history

`git log -p origin/main..HEAD` shows outgoing text, including files later deleted, but only names binaries. List added or modified binaries with:

```bash
git log --diff-filter=AM --numstat --format='commit %h' origin/main..HEAD |
  awk '/^commit /{c=$2; next} /^-\t-\t/{sub(/^-\t-\t/, ""); print c ":" $0}'
```

Inspect each listed `COMMIT:PATH` at that revision, extracting it with `git show COMMIT:PATH` into a temporary file and opening it with an appropriate viewer.

## Recover a failed release

- Retry failed CD jobs using the tested distributions retained for 30 days; do not rebuild successful distributions. The gate's vulnerability scans download advisory data: rerun a job that failed on a download, and fix a newly published advisory before publishing.
- If a GitHub release already exists, creation deliberately fails. Inspect its assets and publication state, then finish a verified draft manually under release authorization. Never replace published assets or move the tag.
- If pushing the tag failed, `push-and-tag` deleted the local tag: rerun `scripts/push-and-tag.sh X.Y.Z`. It repeats its checks, finds the successful CI run and tags the same commit, or stops if origin received the tag after all.
- If `verify-release.sh` reports a digest or attestation mismatch, stop and investigate before announcing the release.
- For a manual documentation deployment, rerun CI on `main`.

Add future release lessons only when they change a concrete step.
