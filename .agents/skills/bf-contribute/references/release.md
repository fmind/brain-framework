# Release checklist

Use only for an explicitly authorized release. The tag-triggered `.github/workflows/cd.yml` owns publication to PyPI and GitHub; do not create a competing release or replace a published tag or asset.

1. Inspect staged, unstaged and untracked work and confirm the intended release scope. Check that `main` matches the remote and that the proposed tag is absent locally and remotely. Preserve unrelated changes.
1. Set the same version in `pyproject.toml` and `src/bf/__init__.py`, refresh `uv.lock` if needed, and add a dated `## [vX.Y.Z]` section in `CHANGELOG.md`. Document breaking changes and update pinned installation examples. `scripts/release-notes vX.Y.Z` must return only that section.
1. Run `mise run generate:schema`, `mise run all` and `dot agent context --check` when available. The gate covers documentation, security, hermetic tests, branch coverage, wheel and source-distribution installation. Inspect formatter changes and qualify the exact candidate. Never weaken checks to publish.
1. Commit the authorized scope with Conventional Commits, keeping hooks enabled. Create an annotated `vX.Y.Z` tag on that commit and push `main` and the tag atomically. The release workflow runs the canonical gate on Linux and macOS, x64 and arm64, before building and attesting both distributions.
1. Wait for exact-commit CI and CD. CD stages a draft, verifies its bytes, publishes through the `pypi` environment’s Trusted Publisher, compares PyPI digests, and only then publishes GitHub. If publication fails, inspect the failed job and reconcile existing remote artifacts before retrying; never move the tag.
1. Verify the remote tag’s peeled commit, non-draft GitHub release, expected wheel and source archive, GitHub build attestations, and matching PyPI SHA-256 digests. Install the published version into an isolated environment and exercise version, initialization, validation, search and exact read. Confirm Pages deployment separately.
1. Report the release URL, version, commit, verified boundaries and remaining limits. Provider freshness and native host routing need their own evidence; release tests use synthetic data and fake providers.

Keep private-brain details out of release notes and repository proposals. Record future release lessons here only when they change a concrete step.
