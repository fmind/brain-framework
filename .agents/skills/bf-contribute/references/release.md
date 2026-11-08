# Release checklist

Use only for an explicitly authorized release. [CD](../../../../.github/workflows/cd.yml) owns PyPI and GitHub publication: never create a competing release, move a tag or replace published assets. Keep private-brain details out of outgoing commits and release notes. [`scripts/release.py`](../../../../scripts/release.py) runs the mechanical steps; this checklist owns the judgment between them. Run long steps in the background.

1. **Write the changelog**: In the primary checkout, complete `## Unreleased` and open it with one paragraph naming what stays unchanged and the upgrade step, such as `bf skills DIR` when a packaged skill changed: “A documentation release. The brain format (`version: 8`), `bf.yaml`, the reply schemas and the search cache are unchanged; no upgrade step is needed.” A breaking change makes the release a major and needs manual upgrade steps.
1. **Prepare the clone**: From the primary checkout on `main`, name the changed paths the release ships, or `--all` when every change belongs to it. Other sessions may hold uncommitted work there, so name paths whenever in doubt:

   ```bash
   python3 scripts/release.py prepare X.Y.Z CHANGELOG.md src/bf/cli.py docs/docs/commands.md
   ```

   It refuses a merge commit, an existing tag or a HEAD behind `origin/main`, then clones HEAD into `~/.cache/bf-release/vX.Y.Z`, copies those paths, bumps the version and dates the changelog section. For a new major it also updates each skill's `compatibility`, regenerates the schemas and refreshes `tests/contract/`; review each line in its `review` reply and update mentions of the previous major that mean the current release, keeping historical ones such as upgrade notes. Work in the clone from now on: pass `-C` or `cd` in the same command, since an agent's shell may reset its directory.
1. **Review the outgoing history**: In the clone, compare `git status --short` with the intended scope and read `git diff --cached -M` and `git log -p origin/main..HEAD`, including files later commits deleted. Git only names binaries: open each `COMMIT:PATH` that the `binaries` reply lists (`staged:PATH` for the release's own files) with an appropriate viewer. Ask a second agent with read-only instructions to review the staged diff: it catches overclaims and stale references that tests miss.
1. **Qualify the candidate**: Run `python3 scripts/release.py check` in the clone: the full gate as CI runs it. Fix failures in the clone, rerun it, and never weaken a check to publish. Then commit with a Conventional Commit, such as `chore(release): release vX.Y.Z`.
1. **Publish**: Run the script file itself, never an inline copy of its commands, which once tagged a failed CI run:

   ```bash
   python3 scripts/release.py publish X.Y.Z
   ```

   It runs [`push-and-tag.sh`](../../../../scripts/push-and-tag.sh), which tags only after CI succeeds on the pushed commit, waits for CD, runs [`verify-release.sh`](../../../../scripts/verify-release.sh) on the published wheel, source archive, digests and attestations, then syncs the primary checkout. The sync stops with nothing changed when another session edited a released path; integrate that checkout by hand, then rerun `python3 scripts/release.py sync`. Remove the clone afterwards. Confirm CI's Pages deployment separately.
1. **Report verified boundaries**: Give the release URL, version, commit, [verified boundaries and remaining limits](../../../../CONTRIBUTING.md#release). Release tests use synthetic data and fake providers; provider freshness and native host routing need separate evidence.

## Recover a failed release

- Before the tag exists, fix the cause in the clone, commit and rerun `publish`: it repeats every check.
- If pushing the tag failed, `push-and-tag.sh` deleted the local tag: rerun `publish`. It tags the same commit, or, if origin received the tag after all, fetches it and continues to CD, verification and sync; a remote tag on another commit stops it.
- Retry failed CD jobs with `gh run rerun ID --failed`, which reuses the tested distributions retained for 30 days. The gate's vulnerability scans download advisory data: rerun a job that failed on a download, and fix a newly published advisory before publishing.
- If a GitHub release already exists, creation deliberately fails. Inspect its assets and state, then finish a verified draft manually under release authorization.
- If `verify-release.sh` reports a digest or attestation mismatch, stop and investigate before announcing the release.

Add future release lessons to the script and its tests, or here only when they change a judgment step.
