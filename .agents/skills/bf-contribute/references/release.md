# Release checklist

Use only for an explicitly authorized release. [CD](../../../../.github/workflows/cd.yml) owns PyPI and GitHub publication; never create a competing release, move a tag or replace published assets. Keep private-brain details out of outgoing commits, release notes and proposals.

1. **Prepare.** Inspect staged, unstaged and untracked work; confirm the release scope and reconcile `main` with the remote. Check that the proposed tag is absent locally and remotely. [Inspect outgoing history](#inspect-outgoing-history), including binaries and files deleted by later commits.
1. **Version.** Run `uv version X.Y.Z` to update `pyproject.toml` and `uv.lock`. Match `src/bf/__init__.py` and every `skills/*/SKILL.md` `metadata.version`; update each skill's `compatibility` for a new major. Move `Unreleased` entries into `## [vX.Y.Z](https://github.com/fmind/brain-framework/releases/tag/vX.Y.Z) - YYYY-MM-DD`, including breaking changes and manual upgrade steps, leaving an empty `## Unreleased` above it. Confirm `scripts/release-notes vX.Y.Z` returns only that section.
1. **Qualify.** Follow the [contribution verification procedure](../../../../CONTRIBUTING.md#set-up-and-check-a-change), including applicable generated files and the full gate, in an isolated worktree. When task settings change, also verify configuration parsing and `mise install --locked` with the workflow-pinned mise version. Run `dot agent context --check` when available. Inspect the final diff and qualify the exact release candidate; never weaken checks to publish.
1. **Publish.** Commit the authorized scope with Conventional Commits and hooks enabled. From the clean release worktree, use the [push and tag block](#push-and-tag): push `main`, wait for CI on that commit across all four platforms, then tag that same commit. Wait for CD to rerun the shared gate and publish its tested artifacts to PyPI and GitHub without rebuilding them.
1. **Verify.** Check the remote tag's peeled commit, non-draft GitHub release, wheel and source archive, GitHub build attestations and matching PyPI SHA-256 digests. Install the published version in an isolated environment; exercise version, initialization, validation, search and exact read. Confirm CI's Pages deployment separately.
1. **Report.** Provide the release URL, version, commit, [verified boundaries and remaining limits](../../../../CONTRIBUTING.md#release). Release tests use synthetic data and fake providers; provider freshness and native host routing require separate evidence.

## Push and tag

Replace `X.Y.Z` with the prepared version. Run the whole block from the clean release worktree: a failed push, missing CI run or unsuccessful CI creates no tag.

```bash
(
  set -eu
  git push origin main
  commit=$(git rev-parse HEAD)
  run=""
  for _ in $(seq 60); do # CI can take a few seconds to list the run
    run=$(gh run list --workflow ci.yml --commit "$commit" --json databaseId -q '.[0].databaseId // empty')
    [ -n "$run" ] && break
    sleep 5
  done
  test -n "$run" # no CI run was listed for this commit
  gh run watch "$run" --exit-status
  conclusion=$(gh run view "$run" --json conclusion -q .conclusion)
  test "$conclusion" = success
  git tag -a vX.Y.Z -m vX.Y.Z "$commit"
  git push origin vX.Y.Z
)
```

## Inspect outgoing history

`git log -p origin/main..HEAD` shows outgoing text, including files later deleted, but only names binaries. List added or modified binaries with:

```bash
git log --diff-filter=AM --numstat --format='commit %h' origin/main..HEAD |
  awk '/^commit /{c=$2; next} /^-\t-\t/{sub(/^-\t-\t/, ""); print c ":" $0}'
```

Inspect each listed `COMMIT:PATH` at that revision, extracting it with `git show COMMIT:PATH` into a temporary file and opening it with an appropriate viewer.

## Recover a failed release

- Retry failed CD jobs using the tested distributions retained for 30 days; do not rebuild successful distributions.
- If a GitHub release already exists, creation deliberately fails. Inspect its assets and publication state, then finish a verified draft manually under release authorization. Never replace published assets or move the tag.
- For a manual documentation deployment, rerun CI on `main`.

Add future release lessons only when they change a concrete step.
