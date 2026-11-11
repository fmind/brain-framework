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
| Wrong brain, or `pass --brain PATH or run inside the brain`       | Execution commands (`collect`, `run`, `channel`, `queue`, `update`, `watch`) act on one brain: `cd` into it or pass `--brain PATH` ([selection](configuration.md#select-a-brain)). |
| `… has no bf.yaml` or `registered brain directory is absent …`    | Point `--brain`, `BF_BRAIN` or `bf register` at the root holding `bf.yaml`.                                                                                                        |
| Unknown CLI option (such as `--no-*`)                             | Check `bf COMMAND -h` and [match docs to your version](upgrades.md#match-the-docs-to-your-version); boolean flags are positive-only (`--check`, `--force`, `--dry-run`, `--link`). |
| `bf.yaml declares version …` or rejected config field             | Apply the [changelog](../changelog.md) upgrade steps and check against `bf schema` ([editor schemas](schema.md#editor-schemas)).                                                   |
| `"valid":false` or `reference exists in several brains`           | Repair each reported `file`/`error` ([checks](checks.md)), or disambiguate with `bf://NAME/...`.                                                                                   |
| Reply carries `problems` or `stale`                               | Repair unreadable/skipped files, or retry once an active writer finishes.                                                                                                          |
| `state directory … is inaccessible` or `may not contain symlinks` | Ensure `~/.local/state/bf` (or `XDG_STATE_HOME`) is a real user-owned directory.                                                                                                   |
| SQLite `< 3.35.0`, PyYAML without libyaml, or missing `mcp` extra | [Reinstall BF](#reinstall-bf).                                                                                                                                                     |

### Reinstall BF

Use the reinstall commands in [Install and update](upgrades.md#install) (`--reinstall --managed-python --python 3.14`, keeping `'brain-framework[mcp]'` if you serve MCP), then restart agent hosts.

## A program fails

Failed collection leaves existing records untouched and marks the program `"failed": true` in `bf status` with `logs/NAME.log` (inspect locally; never paste raw logs publicly).

- **`not an executable regular file`**: run `chmod +x` on the script or invoke its interpreter explicitly in `command`.
- **`set max_text_bytes on this sensor to cut longer text`**: a record's `text` exceeded 4 MiB; filter bloated bodies in the sensor or set `max_text_bytes` on the sensor.
- **`snapshot would remove N of M records`**: verify credentials and scope with `bf collect SENSOR --dry-run` (executes the sensor); if the shrinkage is intentional, pass `--allow-removal` once (`bf update --dry-run` plans without executing).
- **`program was killed by SIGKILL` / `SIGTERM`**: terminated externally (such as OOM killer) rather than by BF's own timeout.

## An assistant fails {#a-prompt-fails}

Preview an expanded prompt safely without running your harness by temporarily declaring `prompt-check: {prompt: prompts/personal.md, command: [cat]}`, running `bf run prompt-check --dry-run` (with `--stdin` if testing input), and removing `prompt-check`. Never edit `command` or `reply` on an assistant with unresolved `bf queue` items ([configuration changes](channels.md#configuration-changes)).

Common assistant and queue errors:

- **`schedule requires either every or at, not both`**: set `every` (seconds) or `at: "HH:MM"` on each schedule entry, never both.
- **`assistant command must print one JSON object when reply is set`** / **`assistant reply at /… is missing or does not match bf schema --kind assistant-reply`**: pass `--output-format json --json-schema "{{schema}}"` and point `reply.path` at the structured output (such as `/structured_output`); when `propose: true`, proposed `actions` must validate as `status: draft` OKF notes.
- **`expanded assistant prompt exceeds 120000 bytes; shorten …`** / **`the argument holding the prompt exceeds Linux's 128 KiB limit`**: shorten the template/input, pass prompts on stdin (omit `{{prompt}}`), and cancel oversized queued input jobs with `bf queue ID --cancel`.
- **Harness could not run `bf read` / `bf search` or failed auth**: include `Bash` in `--tools`, allowlist `Bash(bf read *),Bash(bf search *)`, and sign in under the OS user running `bf watch`.
- **Queue diagnostics** ([queue recovery](channels.md#queue-and-recovery)):
  - `assistant queue is full; …` or `admitted N of M messages` — resolve `failed`/`uncertain` items in `bf queue`.
  - `assistant queue lacks answer space for this job; …` — waits automatically until running jobs free space (`bf run` cancels its run; retry afterwards).
  - `assistant queue holds unresolved work admitted under this brain's previous path or name; …` — restore the previous path/name, drain `bf queue`, then move/rename again.
  - `assistant queue predates this release; …` or `unexpected assistant queue schema; …` — preserve `queue.sqlite3` and follow the [changelog](../changelog.md) upgrade steps using the release that created it.
  - `assistant queue holds an interrupted write; …` — keep `queue.sqlite3` and `queue.sqlite3-journal` together and run `bf queue` to roll back cleanly.

## Updates do not run

```bash
bf update --dry-run
bf status
```

Verify the program is `enabled: true`, sets a nonzero `refresh` or `at` (or `schedules`/`inputs` on an assistant), matches any CLI selectors, is not skipped as `gated` by a `when:` condition that currently has no matching events, and has an active `bf watch` service or timer on **this** machine ([timing and health](schedule.md#timing-and-health)).

## Recover the cache or an interrupted write

When `bf status` reports `"pending_transaction": true` or a stale/damaged `.bf/` cache, wait for active collection to finish and run:

```bash
bf build
bf validate
bf status --check
```

`bf build` rolls back interrupted record writes from `memories/.pending/` hard-link backups and atomically rebuilds `.bf/index.sqlite`. If `memories/.pending` itself is corrupted (`unexpected file in memories/.pending` or `invalid memories/.pending manifest`), move the named stray file or `memories/.pending` outside the brain, run `bf build && bf validate`, and recollect that source.

For file-level issues:

- **Manual edits inside `memories/`**: run `bf build` so the cache re-indexes in-place edits.
- **Symlinks, non-UTF-8 notes or Git conflict markers**: replace symlinks inside the brain with regular files/folders, convert notes to UTF-8, remove `.write-HEX` leftovers in note folders, and reconcile `<<<<<<<` conflict markers so notes re-enter search.

## Report a reproducible problem

Include `bf --version`, OS, exact command (with private paths redacted), exit code and minimal synthetic reproduction — never private notes, credentials or raw `logs/`. File bugs in [GitHub issues](https://github.com/fmind/brain-framework/issues) and security reports via [SECURITY.md](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
