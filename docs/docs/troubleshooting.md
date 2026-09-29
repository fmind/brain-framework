---
description: Diagnose installation, retrieval, collection and agent connection problems without losing evidence.
---

# Troubleshooting

Start inside the intended brain directory. These commands inspect saved files and local history without running sensors or contacting providers:

```bash
bf --version
bf validate
bf status
```

Read the named file, field or program in the diagnostic before retrying. `bf status --check` gives scripts a failing exit code when health checks fail.

## Find the right fix

| Symptom                                           | First check                                                         | Next step                                                                                                          |
| ------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `bf: command not found`                           | Did uv finish installing BF?                                        | Run `uv tool update-shell`, open a new shell and retry `bf --version`.                                             |
| No brain, or the wrong brain                      | Working directory and `BF_BRAIN`.                                   | Use `bf read --brain ~/brain`; see [selection precedence](configuration.md#select-a-brain).                        |
| `pass --brain PATH or run inside the brain`       | Did `update`, `collect`, `watch` or `schedule` run outside a brain? | Run inside it or pass `--brain PATH`; registration selects brains for retrieval only.                              |
| An option is unknown                              | `bf --version` and `bf COMMAND --help`.                             | [Match the docs to your version](upgrades.md#match-the-docs-to-your-version).                                      |
| A configuration field is rejected                 | Named file and field.                                               | Fix its type, spelling or duplicate YAML key; use the [installed schema](schema.md#editor-schemas).                |
| `bf validate` returns `"valid":false`             | Each `problems` entry's `file` and `error`.                         | Repair those files and validate again; see [Check your brain](checks.md).                                          |
| Search misses a known answer                      | Read the exact note or record ref.                                  | Try words from that evidence without a scope; see [word matching](retrieval.md#word-matching).                     |
| Search returns nothing with `problems` or `stale` | Skipped files, references or an active writer.                      | Resolve them and repeat; an incomplete result cannot establish absence.                                            |
| `reference exists in several brains`              | Several selected brains hold that ref.                              | Read the result's `uri`, a `bf://NAME/...` address.                                                                |
| An exact read returns `offset` and `next_offset`  | The reply exceeds 32 KiB, so its text arrives in pages.             | Read one section from its `outline`, or [follow the text pages](retrieval.md#large-exact-reads).                   |
| A sensor fails                                    | `bf status` and the indicated private log.                          | Review its command, credentials, time window and output bounds; [sensor recovery](#a-sensor-fails).                |
| A sensor never runs automatically                 | `enabled`, `refresh` and program selectors.                         | Zero refresh means manual; [preview due work](#updates-do-not-run).                                                |
| An agent cannot connect                           | The same exact read in your terminal.                               | Check absolute executable/brain paths and restart the host; [MCP troubleshooting](mcp.md#if-the-connection-fails). |

## A sensor fails

Failed collection leaves existing records intact. `bf status` marks the source `"failed":true` with its `error`, consecutive `failures` and private `log`. Inspect that log locally: it can contain provider output, so do not paste it into a public issue. Correct the cause before rerunning the named sensor.

Updates retry a failed program after 1 minute, then 2, 4, 8… minutes, never waiting longer than its `refresh`. `bf collect SENSOR` retries a sensor immediately.

`bf collect SENSOR --dry-run` **executes the sensor**, may contact its provider and returns sample evidence. Use it only after reviewing that source and its scope. In contrast, `bf update --dry-run` only lists due work.

A snapshot cannot replace a nonempty catalog with nothing, or remove more than half of it and more than 10 records, so it fails with `snapshot would remove N of M records`. Check the account, folder and provider response; do not delete stored evidence to make collection pass. When the removal is intended, accept it for one run with `bf collect SENSOR --allow-removal`; see [collection modes](sensors.md#define-the-scope-before-adding-a-sensor).

## Updates do not run

```bash
bf update --dry-run
bf status
```

A program must be enabled, have a nonzero `refresh` and be due. Selectors can exclude it. The preview runs nothing; an empty due list may be correct. Watch must remain open, or the native scheduler must be active on a running host. See [timing and health](schedule.md#timing-and-health).

Check health on the collecting machine. A teammate's clone has the shared evidence but not your local collection history. `bf build` cannot refresh provider data.

## Recover the cache or an interrupted write

Let active collection finish. Keep notes, records and any `memories/.pending/` journal; back up the idle brain before recovery. Then run:

```bash
bf build
bf validate
bf status --check
```

`build` recovers interrupted record writes and reconstructs the disposable cache. It exits 1 when its `skipped` count shows unreadable files; `bf validate` names them. Validation should return `"valid":true`; health can still fail if a scheduled source needs attention. Read the remaining diagnostic rather than deleting the pending journal. See [file safeguards](limits.md#files).

`bf update`, and therefore `bf watch`, also recovers an interrupted record write before refreshing the cache; search and read refuse to apply the journal and name both commands. `build` fills the new cache beside the current one, as `.bf/index.sqlite.new`, and replaces it only when complete: searches keep answering from the current cache and collections keep committing meanwhile, and an interrupted build leaves the current cache intact. Files changed during the build are indexed again by the next search or update. If `build` reports that readers kept the search cache open, let long-running reads finish, then repeat it.

## Report a reproducible problem

Include BF version, operating system, the failing command with private paths replaced, exit code and expected versus observed behavior. Use a small fictional note or record that reproduces the issue. Omit private passages, credentials and provider logs.

Use [GitHub issues](https://github.com/fmind/brain-framework/issues) for ordinary bugs and the [private reporting instructions](https://github.com/fmind/brain-framework/blob/main/SECURITY.md) for suspected vulnerabilities.
