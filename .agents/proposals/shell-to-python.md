# Shell-to-Python migration

Status: draft for review. This proposal changes no executable or configuration.

## Decision

Adopt a selective Python-first policy, not a Python-only rule. Convert helpers that own structured data, pagination, privacy, path safety, or transactional behavior during the current FKF v5 Python transition. Keep small process launchers, simple projections, and black-box integration tests in shell where Python would add ceremony without removing risk.

## Current evidence

- FKF has 46 tracked shell executables totaling 4,774 lines. Its 25 scripts of at least 80 lines contain 4,093 of those lines.
- Brain has 65 tracked shell executables totaling 6,930 lines. Forty of its 46 `bin/` shell helpers are byte-identical FKF-managed copies, so they should never be migrated independently.
- Both repositories' ShellCheck gates pass. FKF's 155 asset/helper tests also pass.
- FKF requires Python 3.14 and Brain pins Python 3.14. Python is therefore already a first-class runtime, although every standalone Python helper must still declare `python3` in `requires:`.
- The current tests and asset loader already support `.py` helpers. The main cost is preserving behavior and updating exact helper names, presets, fixtures, tests, documentation, and Brain.

## Options

| Option                                  | What to do                                                                                                                               | Benefit                                                                                                                         | Cost and trade-off                                                                                                                                |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Selective hard cut (recommended)** | Convert the 34 logic-heavy FKF helpers and six Brain-only production/check helpers below, in verified clusters; retain small shell glue. | Typed data handling, clearer failure paths, fewer quoting/FIFO/temp-file hazards, and one language for FKF's substantive logic. | Large review surface; helper filenames and trust digests change; some `jq`/`yq` dependencies remain in retained glue.                             |
| **2. Convert only on touch**            | Leave every passing helper unchanged until it needs a behavior change.                                                                   | Lowest immediate risk and effort.                                                                                               | Preserves about 4,000 lines of complex public shell through the v5 transition and makes later no-shim renames more disruptive for bases.          |
| **3. Python-only**                      | Convert every shell file, including launchers, tiny wrappers, and tests.                                                                 | Uniform language and tooling.                                                                                                   | Replaces concise UNIX glue with more code, weakens direct process-boundary tests, and adds migration risk without proportional correctness value. |

## Proposed scope

### Convert FKF's substantive helpers

- [ ] **Local evidence and trust:** `agent-memory-body.sh`, `agent-memory-files.sh`, `agent-prompt-body.sh`, `agent-prompts.sh`, `agent-session-trace.sh`, `agent-sessions.sh`, `atuin-history-json.sh`, `chrome-bookmarks.sh`, `chromium-pages.sh`, `fkf-hook.sh`, `git-log-json.sh`, and `writing-index.sh`.
- [ ] **GitHub:** `github-commits-json.sh`, `github-events-json.sh`, `github-generic-list-json.sh`, `github-list-json.sh`, `github-reviews-json.sh`, `github-search-json.sh`, and `gh-runs.sh`.
- [ ] **Other providers:** `gcloud-audit-json.sh`, `gmail-json.sh`, `gws-calendar-body.sh`, `gws-calendars-json.sh`, `gws-chat-message-body.sh`, `gws-chat-messages.sh`, `gws-meeting-notes-json.sh`, `gws-page-json.sh`, `gws-tasks.sh`, `huggingface-repositories-json.sh`, `jira-issues-json.sh`, `kaggle-json.sh`, `kaggle-kernels-json.sh`, and `kaggle-models-json.sh`.
- [ ] **Feeds:** `rss-json.sh`.

These scripts contain stateful loops, recursive pagination, multiple structured-data transforms, security/privacy filtering, or filesystem traversal. Python should use direct `subprocess` argv, bounded temporary files or streams, strict JSON decoding, explicit dataclasses or typed dictionaries at provider boundaries, and no `shell=True`.

### Convert Brain-only production logic

- [ ] Convert `bin/drive-sync.sh`: 711 lines, extensive JSON state reconciliation, atomic publish, path validation, and rollback behavior.
- [ ] Convert `checks/evidence-archive.sh`: archive selection, manifests, hashing, tar validation, encryption, and restore confinement already cross into embedded Python.
- [ ] Convert `bin/google-developers-json.sh`, `bin/gcloud-audit-projects-json.sh`, `bin/gcloud-billing-json.sh`, and `bin/brain-drive-files-json.sh`: each parses or combines provider data and enforces completeness/security constraints.

### Keep shell

- [ ] Keep FKF's `scripts/install-sast.sh`, `scripts/release-notes`, and demo helper: they are short repository/process orchestration, and the demo intentionally shows a minimal executable source.
- [ ] Keep the small bundled wrappers `gcloud-auth-ready.sh`, `writing-source-json.sh`, `mise-tools-json.sh`, `kaggle-competitions-json.sh`, `kaggle-datasets-json.sh`, `gws-doc-text.sh`, `gcloud-projects-json.sh`, `github-gists-json.sh`, and `github-stars-json.sh`. Reassess only when their behavior grows.
- [ ] Keep Brain's `bin/brain-writing-source-json.sh` and `checks/fkf-schedule-launcher.sh`; their purpose is shell expansion or one final `exec`.
- [ ] Keep Brain's source-hook tests and `checks/drive-sync.sh` / `checks/evidence-recovery.sh` as black-box shell tests initially. Convert a test only when shell obscures the assertion, not merely because its subject moved to Python.
- [ ] Keep small shell blocks in `mise.toml` where they express task orchestration rather than application logic.

## Migration contract

- [ ] Rename selected helpers directly from `.sh` to `.py`; do not ship forwarding shell shims or two spellings.
- [ ] Keep each helper standalone and standard-library-first. Do not add a helper framework, provider SDK, second package, or shared module unless repeated code proves a concrete need.
- [ ] Keep provider authentication in provider CLIs and invoke every child with explicit argv.
- [ ] Preserve exact exit codes, bounded output, finite page ceilings, deduplication, half-open time windows, privacy projections, symlink refusals, and all-or-nothing stdout.
- [ ] Update FKF presets, `requires:`, probes, helper inventory, fixtures, docs, and tests in the same cluster. Add `python3` explicitly for every source that invokes a Python helper.
- [ ] Refresh Brain's forty managed copies only from the reviewed FKF assets, then update its local helpers, `fkf.yaml`, source hooks, task vocabulary, and trust together.
- [ ] Do not re-collect durable evidence. The migration may re-arm execution trust, but stored JSON and Markdown remain valid.

## Suggested slices after approval

- [ ] **P1, pattern:** migrate `gws-page-json.sh`, `kaggle-json.sh`, and `writing-index.sh` to prove subprocess, JSON, pagination, and standalone-interpreter conventions.
- [ ] **P2, public helpers:** migrate the remaining FKF groups with focused fake-provider tests after each group, then run `mise run all`.
- [ ] **P3, Brain managed surface:** refresh exact FKF helper bytes and update all corresponding `run:`, `body:`, `test:`, and `requires:` entries without touching retained evidence.
- [ ] **P4, Brain-only logic:** migrate Drive/recovery/provider helpers, retain the current black-box checks, then run `mise run check` and `mise run test`.
- [ ] **P5, qualification:** compare representative old/new helper outputs using synthetic fixtures, verify no provider command runs in tests, review trust-plan changes, and keep release/push/live collection as separate approvals.

## Acceptance rule

The migration is worthwhile only if it reduces shell logic and dependencies without increasing the number of concepts or weakening a failure shield. If a Python replacement is longer but only wraps one command and one `jq` projection, keep the shell version.
