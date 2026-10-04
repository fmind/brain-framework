# Release checklist

Use only for an explicitly authorized release. [CD](../../../../.github/workflows/cd.yml) owns PyPI and GitHub publication; never create a competing release, move a tag or replace published assets. Keep private-brain details out of outgoing commits, release notes and proposals.

1. **Prepare.** Inspect staged, unstaged and untracked work; confirm the release scope and reconcile `main` with the remote. Check that the proposed tag is absent locally and remotely. [Inspect outgoing history](#inspect-outgoing-history), including binaries and files deleted by later commits.
1. **Version.** Run `uv version X.Y.Z` to update `pyproject.toml` and `uv.lock`. Match `src/bf/__init__.py`. For a new major, update each `src/bf/skills/*/SKILL.md` `compatibility`, which `tests/test_skills.py` checks, then search the skills, examples and docs for the previous major: update the mentions that mean the current release and keep historical ones, such as upgrade notes. Before 18.0.0, for example:

   ```bash
   git grep -nE '(Brain Framework|bf`?) 17\b|\b17\.[0-9]+\.[0-9]+' -- src/bf/skills examples docs/docs README.md
   ```

   Move `Unreleased` entries into `## [vX.Y.Z](https://github.com/fmind/brain-framework/releases/tag/vX.Y.Z) - YYYY-MM-DD`, including breaking changes and manual upgrade steps, leaving an empty `## Unreleased` above it. Confirm `scripts/release-notes.sh vX.Y.Z` returns only that section: `check:changelog` catches a missing, duplicate or empty section, not another heading, such as `## Unreleased`, placed below it.
1. **Contract.** A major release refreshes the contract after `mise run generate:schema`, with `cp docs/*.schema.json tests/contract/`, so `tests/test_contract.py` guards the new contract. A minor or patch release keeps `tests/contract/` unchanged, so the gate rejects [any incompatible schema change](../../../../CONTRIBUTING.md#keep-the-contract).
1. **Qualify.** Follow the [contribution verification procedure](../../../../CONTRIBUTING.md#set-up-and-check-a-change), including applicable generated files and the full gate, in an isolated worktree. When task settings change, also verify configuration parsing and `mise install --locked` with the workflow-pinned mise version. Run `dot agent context --check` when available. Inspect the final diff and qualify the exact release candidate; never weaken checks to publish.
1. **Publish.** Commit the authorized scope with Conventional Commits and hooks enabled. From the clean release worktree, [push and tag](#push-and-tag): `scripts/push-and-tag.sh X.Y.Z` pushes `main`, waits for CI on that commit across all four platforms, then tags that same commit. Wait for CD to rerun the shared gate and publish its tested artifacts to PyPI and GitHub without rebuilding them.
1. **Verify.** Check the remote tag's peeled commit, non-draft GitHub release, wheel and source archive, GitHub build attestations and matching PyPI SHA-256 digests. Install the published version in an isolated environment; exercise version, initialization, validation, search and exact read. Confirm CI's Pages deployment separately.
1. **Report.** Provide the release URL, version, commit, [verified boundaries and remaining limits](../../../../CONTRIBUTING.md#release). Release tests use synthetic data and fake providers; provider freshness and native host routing require separate evidence.

## Push and tag

From the clean release worktree on `main`, run the script as its own command, never after `cd … &&` or inside another script:

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
- For a manual documentation deployment, rerun CI on `main`.

Add future release lessons only when they change a concrete step.
