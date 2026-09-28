# Release checklist

Use only for an explicitly authorized release. The tag-triggered `.github/workflows/cd.yml` owns publication to PyPI and GitHub; do not create a competing release or replace a published tag or asset.

1. Inspect staged, unstaged and untracked work and confirm the intended release scope. Qualify from a clean worktree of the release commit, since other sessions may edit a shared checkout. Check that `main` matches the remote, that the proposed tag is absent locally and remotely, and that the commits to push add no private-brain data. `git log -p origin/main..HEAD` shows their text, including files they later delete, but only names binary files; open each image, archive or other binary that this lists as `COMMIT:PATH` with `git show COMMIT:PATH`:

   ```bash
   git log --diff-filter=AM --numstat --format='commit %h' origin/main..HEAD |
     awk '/^commit /{c=$2; next} /^-\t-\t/{sub(/^-\t-\t/, ""); print c ":" $0}'
   ```

1. Run `uv version X.Y.Z`: it sets `pyproject.toml` and relocks `uv.lock`, which records the project version. Set the same version in `src/bf/__init__.py` and in each `skills/*/SKILL.md` `metadata.version`, and name the new major in each `compatibility`; `tests/test_skills.py` checks both. Move the `Unreleased` entries into a new `## [vX.Y.Z](https://github.com/fmind/brain-framework/releases/tag/vX.Y.Z) - YYYY-MM-DD` section, document breaking changes there and leave an empty `## Unreleased` heading above it. `scripts/release-notes vX.Y.Z` must return only that section; missing, empty or duplicate sections fail without emitting release notes.
1. Check that the workflow-pinned mise version can parse the repository configuration and run `mise install --locked`; validate with that version before tagging when task settings change. Run `mise run generate:schema`, `mise run generate:notices` and, after dashboard or Rich changes, `mise run generate:screenshot`; then run `mise run format` and inspect the diff. Follow the [contribution verification procedure](../../../../CONTRIBUTING.md#set-up-and-check-a-change) and run `dot agent context --check` when available. Qualify the exact release candidate; never weaken checks to publish.
1. Commit the authorized scope with Conventional Commits, keeping hooks enabled. Push `main`, wait for exact-commit CI to pass on all four platforms, then tag that same commit. This catches runner-specific failures before reserving an immutable version. Run the block as a whole: its subshell stops at the first failure, so a failed push, an unlisted run or red CI creates no tag:

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

1. Wait for CD. It first reruns `verify.yml`, the [shared gate](../../../../CONTRIBUTING.md#maintain-the-shared-gate): the check job requires the tag to equal `v$(uv version --short)` and valid release notes before `mise run check`; the Linux, macOS, x64 and arm64 jobs then run `mise run test` and `mise run build`, and the Linux x64 job uploads the smoke-tested wheel and source archive with their SHA-256 list for 30 days. Publication never rebuilds them. The `pypi` job (Trusted Publisher environment, `v*` tags only) checks the hashes and the file count, runs `scripts/verify-release-tag` (the remote tag must name this commit and `main` must contain it), attests the files, uploads them with `skip-existing` and compares PyPI's digests. The GitHub job repeats the hash and tag checks, creates a draft from `scripts/release-notes`, compares the downloaded draft assets and publishes it. Retry failed jobs rather than rebuilding successful distributions. A pre-existing GitHub release fails creation deliberately: inspect its assets and publication state, then finish the verified draft manually under release authorization. Never replace published assets or move the tag.
1. Verify the remote tag’s peeled commit, non-draft GitHub release, expected wheel and source archive, GitHub build attestations, and matching PyPI SHA-256 digests. Install the published version into an isolated environment and exercise version, initialization, validation, search and exact read. Confirm the CI workflow’s Pages deployment separately; rerun CI on `main` for a manual documentation deployment.
1. Report the release URL, version, commit, verified boundaries and remaining limits. Provider freshness and native host routing need their own evidence; release tests use synthetic data and fake providers.

Keep private-brain details out of release notes and repository proposals. Record future release lessons here only when they change a concrete step.
