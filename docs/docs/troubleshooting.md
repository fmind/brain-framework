---
icon: lucide/life-buoy
description: Diagnose installation, retrieval, collection and file problems without losing evidence.
---

# Troubleshooting

Start inside the intended brain and inspect saved state offline before retrying:

```bash
bf --version
bf validate
bf status
```

Diagnostics write one `bf:` line to stderr naming the file, field or program without quoting private content.

## Quick lookup

| Symptom                                                           | Fix                                                                                                                                                                                |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bf: command not found`                                           | Run `uv tool update-shell` and open a new shell.                                                                                                                                   |
| Wrong brain, or `pass --brain PATH or run inside the brain`       | `cd` into the brain, pass `--brain PATH`, set `BF_BRAIN`, or mark a registry default with `bf register --default` ([selection](configuration.md#select-a-brain)).                  |
| `… has no bf.yaml` or `registered brain directory is absent …`    | Point `--brain`, `BF_BRAIN` or `bf register` at the root holding `bf.yaml`.                                                                                                        |
| Unknown CLI option (such as `--no-*`)                             | Check `bf COMMAND -h` and [match docs to your version](upgrades.md#match-the-docs-to-your-version); boolean flags are positive-only (`--check`, `--force`, `--dry-run`, `--link`). |
| `bf.yaml declares version …` or rejected config field             | Apply the [changelog](../changelog.md) upgrade steps and check against `bf schema` ([editor schemas](schema.md#editor-schemas)).                                                   |
| `"valid":false`                                                   | Repair each reported `file`/`error` ([checks](checks.md)).                                                                                                                         |
| Reply carries `problems` or `stale`                               | Repair unreadable/skipped files, or retry once an active writer finishes.                                                                                                          |
| `state directory … is inaccessible` or `may not contain symlinks` | Ensure `<brain>/.bf` (and `.bf/state`, `.bf/locks`) is a real user-owned mode-`700` directory with no symlinks.                                                                    |
| SQLite `< 3.35.0`, PyYAML without libyaml, or missing `mcp` extra | [Reinstall BF](#reinstall-bf).                                                                                                                                                     |

### Reinstall BF

Use the reinstall commands in [Install and update](upgrades.md#install) (`--reinstall --managed-python --python 3.14`, keeping `'brain-framework[mcp]'` if you serve MCP), then restart agent hosts.

## A job fails

Failed collection leaves existing records untouched and marks the job `"failed": true` in `bf status` with `logs/NAME.log` (inspect locally; never paste raw logs publicly).

- **`not an executable regular file`**: run `chmod +x` on the script or invoke its interpreter explicitly in `run`/`harness`/`send`/`gate` (run `bf doctor` to check all job binaries, scripts and `requires.env` variables).
- **`set max_text_bytes on this job to cut longer text`**: a record's `text` exceeded 4 MiB; filter bloated bodies in the job or set `max_text_bytes` on that job.
- **`snapshot would remove N of M records`**: verify credentials and scope with `bf collect JOB --dry-run` (executes the job); if the shrinkage is intentional, pass `--allow-removal` once (`bf update --dry-run` plans without executing).
- **`program was killed by SIGKILL` / `SIGTERM`**: terminated externally (such as OOM killer) rather than by BF's own timeout.

## A prompt or queue job fails {#a-prompt-fails}

Preview an expanded prompt safely without running your harness by temporarily declaring `prompt-check: {prompt: prompts/personal.md, harness: [cat]}`, running `bf run prompt-check --dry-run` (with `--stdin` if testing input), and removing `prompt-check`. Never edit or disable a `send:` job while deliveries await `bf queue` ([configuration changes](channels.md#configuration-changes)).

Common trigger and queue errors:

- **`trigger accepts either every or at, not both`**: set `every` (seconds) or `at: "HH:MM"` on each `on:` trigger, never both.
- **`extract` pointer failure or invalid action Markdown**: inspect raw output with `bf run JOB --dry-run` (omitting `extract` returns stdout verbatim; `emit: action` requires `type: action` and `status: draft` OKF frontmatter).
- **`expanded prompt exceeds 120000 bytes`** / **`expanded argument contains NUL or exceeds 128 KiB`**: shorten the prompt template, pass prompt stdin (omit `{{prompt}}`), and inspect `bf queue`.
- **Harness could not run `bf read` / `bf search` or failed auth**: include `Bash` in `--tools`, allowlist `Bash(bf read *),Bash(bf search *)`, and sign in under the OS user running `bf watch`.
- **Queue diagnostics** ([queue recovery](channels.md#queue-and-recovery)):
  - `runtime queue has no unresolved-work slots; clear pending work first` or `runtime queue reached its size limit; resolve or clear pending work` — resolve `failed`/`uncertain` items in `bf queue`.
  - `send job configuration changed or is disabled; inspect bf queue before retrying` — restore the `send:` configuration or cancel the pending delivery with `bf queue ID --cancel`.
  - `runtime queue holds unresolved work under a previous path or name; resolve via bf queue first` — restore the previous path/name, drain `bf queue`, then move/rename again.
  - `runtime database predates this release; preserve it and follow upgrade steps in the changelog` or `unexpected runtime database schema; preserve it and repair it` — preserve `.bf/runtime.sqlite3` and follow the [changelog](../changelog.md) upgrade steps using the release that created it.
  - `runtime database holds an interrupted write; run bf queue to recover it` — keep `.bf/runtime.sqlite3` and `.bf/runtime.sqlite3-journal` together and run `bf queue` to roll back cleanly.

## Updates do not run

```bash
bf update --dry-run
bf status
```

Verify the job is `enabled: true`, sets a nonzero `on.every` or `on.at` (or `on.event`), matches any `--job` selectors, is not skipped as `gated` by a `gate:` script exiting nonzero, and has an active `bf watch` service or timer on **this** machine ([timing and health](schedule.md#timing-and-health)).

## Recover the cache or an interrupted write

When `bf status` reports `"pending_transaction": true` or a stale/damaged `.bf/` cache, wait for active collection to finish and run:

```bash
bf build
bf validate
bf status --check
```

`bf build` rolls back interrupted record writes from `memories/.pending/` hard-link backups and atomically rebuilds `.bf/cache.sqlite3`. If `memories/.pending` itself is corrupted (`unexpected file in memories/.pending` or `invalid memories/.pending manifest`), move the named stray file or `memories/.pending` outside the brain, run `bf build && bf validate`, and recollect that source.

For file-level issues:

- **Manual edits inside `memories/`**: run `bf build` so the cache re-indexes in-place edits.
- **Symlinks, non-UTF-8 notes or Git conflict markers**: replace symlinks inside the brain with regular files/folders, convert notes to UTF-8, remove `.write-HEX` leftovers in note folders, and reconcile `<<<<<<<` conflict markers so notes re-enter search.

## Report a reproducible problem

Include `bf --version`, OS, exact command (with private paths redacted), exit code and minimal synthetic reproduction — never private notes, credentials or raw `logs/`. File bugs in [GitHub issues](https://github.com/fmind/brain-framework/issues) and security reports via [SECURITY.md](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
