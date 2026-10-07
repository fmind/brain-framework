---
icon: lucide/life-buoy
description: Diagnose installation, retrieval, collection and file problems without losing evidence.
---

# Troubleshooting

Start inside the intended brain. These commands inspect saved files and local history, without running programs or contacting providers:

```bash
bf --version
bf validate
bf status
```

Read the file, field or program the diagnostic names before retrying. Diagnostics go to stderr and never quote your content.

## Find the right fix

| Symptom                                     | First check                                                                | Next step                                                                                                                               |
| ------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `bf: command not found`                     | Did uv finish installing BF?                                               | Run `uv tool update-shell`, open a new shell and retry `bf --version`.                                                                  |
| No brain, or the wrong one                  | Working directory and `BF_BRAIN`.                                          | Pass `--brain ~/brain`; see [brain selection](configuration.md#select-a-brain).                                                         |
| `pass --brain PATH or run inside the brain` | Did `collect`, `run`, `update`, `watch` or `schedule` run outside a brain? | Run inside it or pass `--brain PATH`; registration selects brains for retrieval only.                                                   |
| `… has no bf.yaml`                          | Does `--brain`, `BF_BRAIN` or the registry name a subfolder or parent?     | Name the brain's root folder, which holds `bf.yaml`, or create a brain with `bf init`.                                                  |
| `registered brain directory is absent …`    | Did the brain move, or is its disk unmounted?                              | Restore it, run `bf register` at its new place or remove its entry; see [registration](configuration.md#optional-machine-registration). |
| `bf: invalid input: ARGUMENT: …`            | The named argument in `bf COMMAND -h`.                                     | Correct that value and retry; see [exit codes](commands.md#exit-codes-and-errors).                                                      |
| An option is unknown                        | `bf --version` and `bf COMMAND --help`.                                    | [Match the docs to your version](upgrades.md#match-the-docs-to-your-version).                                                           |
| `bf.yaml declares version …`                | The brain format in `bf.yaml` and `evals/*.yaml`.                          | Apply the upgrade steps in the [changelog](../changelog.md).                                                                            |
| A configuration field is rejected           | The named file and field.                                                  | Fix its type, spelling or duplicate key; use the [editor schema](schema.md#editor-schemas).                                             |
| `reference not found; … did you mean …?`    | The suggested name.                                                        | Read the suggestion, or search for the evidence. A missing section lists the note's sections.                                           |
| `"valid":false`                             | Each problem's `file` and `error`.                                         | Repair them and validate again; see [Check your brain](checks.md).                                                                      |
| Search misses a known answer                | The exact note or record ref, and `unmatched`.                             | Use words from that evidence without a scope; see [search](search.md#search).                                                           |
| A reply has `problems` or `stale`           | Skipped files, references or a busy writer.                                | Resolve them, or retry after the writer finishes.                                                                                       |
| `reference exists in several brains`        | Several selected brains hold that ref.                                     | Read the result's `uri`, a `bf://NAME/...` address.                                                                                     |
| A program fails                             | `bf status` and its `logs/NAME.log`.                                       | See [a program fails](#a-program-fails).                                                                                                |
| A program never runs automatically          | `enabled`, `refresh` and selectors.                                        | See [updates do not run](#updates-do-not-run).                                                                                          |
| `state directory … is inaccessible`         | The named directory and `XDG_STATE_HOME`.                                  | Make it yours and writable, or set `XDG_STATE_HOME`; see [local state](configuration.md#local-state).                                   |
| `state directory may not contain symlinks`  | Whether `~/.local/state`, `XDG_STATE_HOME` or its `bf/` folder is a link.  | Replace the link with a real folder, or set `XDG_STATE_HOME` to one; see [local state](configuration.md#local-state).                   |
| `the search cache needs SQLite 3.35.0 …`    | The SQLite version the message names.                                      | Reinstall BF on a uv-managed Python: `uv tool install --reinstall --managed-python --python 3.14 brain-framework`.                      |
| An agent cannot connect                     | The same read in your terminal.                                            | Check absolute paths and restart the host; see [MCP troubleshooting](mcp.md#if-the-connection-fails).                                   |

## A program fails

Failed collection leaves saved records intact. `bf status` marks the program `"failed":true` with its `error`, consecutive `failures` and `log`, such as `logs/calendar.log`. The log can hold provider output: read it locally and never paste it into a public issue. Fix the cause, then rerun the program with `bf collect SENSOR` or `bf run ROUTINE`; updates retry on their own after the failure backoff.

`bf collect SENSOR --dry-run` **executes the sensor** and may contact its provider; `bf update --dry-run` only lists due work. A program that `bf validate` reports as `not an executable regular file` needs `chmod +x`, or an interpreter in its `command`. Behind an interpreter, as in `[uv, run, --no-project, sensors/brief.py]`, validation checks only that the first `sensors/` or `routines/` path among the arguments exists: `is not a regular file or folder` points to a missing file or a typo. `program was killed by SIGKILL` means another process, such as the out-of-memory killer, ended the program.

A snapshot fails with `snapshot would remove N of M records` when it would empty its source, or remove more than half of it and more than 10 records. Check the account, folder and provider response; do not delete stored records to make collection pass. When the removal is intended, accept it once with `bf collect SENSOR --allow-removal`.

## Updates do not run

```bash
bf update --dry-run
bf status
```

A program runs automatically only when it is enabled, has a nonzero `refresh`, is due and matches the selectors. The dry run's `manual` list names enabled programs with `refresh: 0`. Watch must stay open, or the native scheduler must be active on a running machine; see [timing and health](schedule.md#timing-and-health). Check health on the collecting machine: a teammate's clone shares the records, not the run history.

## Recover the cache or an interrupted write

While an interrupted record write awaits recovery, `bf status` reports `"pending_transaction":true`, a `stale` or `missing` [cache](commands.md#status-sources-and-routines) and a problem naming the command to run. Let active collection finish, then:

```bash
bf build
bf validate
bf status --check
```

`bf build` recovers the interrupted write and rebuilds the cache from the files. Before replacing records, a write keeps hard-link backups of them in `memories/.pending/`. Recovery restores only the files that write changed, so it takes about a second; keep that folder until it succeeds. `bf collect`, `bf update` and `bf watch` also recover it before writing; search and read never do.

The rebuild fills `.bf/index.sqlite.new` beside the live cache and replaces it when complete: searches keep answering meanwhile. A rebuild killed before it finishes leaves that file, which the next search, read or status removes once no build is running. `bf build` exits 1 when its `skipped` count shows unreadable files; `bf validate` names them. A damaged cache is discarded and rebuilt on its own. An exact read that meets an active write returns within about 3 seconds, marked `stale`: retry it for the final content.

## Files

| If you see…                                                                    | What to do                                                                                                                                              |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A skipped link or special file in `problems`                                   | Use a regular file inside the brain; BF never follows links.                                                                                            |
| `unreadable folder` or `inaccessible file or folder`                           | Restore read and search permission, such as with `chmod -R u+rwX memories`, or move the folder out of the brain; the rest of the brain keeps answering. |
| `unreadable file; retried next time`                                           | A read failed, such as on a network or sync mount: the next search, read or status tries the file again. If it persists, check the mount or disk.       |
| A file name that is not valid UTF-8, or holds a backslash or control character | Rename the file, shown with escapes such as `\xNN`, as for a Latin-1 name from an old archive.                                                          |
| `expected a directory; symlinks and special files are not followed`            | Replace the linked folder with a regular one.                                                                                                           |
| `note is not UTF-8`                                                            | Convert the named note to UTF-8.                                                                                                                        |
| Git conflict markers at the start of a line                                    | Reconcile both revisions: the note stays unsearchable until then, even inside a code block.                                                             |

Writes are atomic. A write killed before its rename, by BF or the bf-use guarded-write helper, leaves a `.write-HEX` file beside its target. The next write of a record source removes its own; `bf validate` warns about those in `projects/`, `concepts/` and `actions/` (`an interrupted write left this temporary file; delete it`), and none blocks an action routine. An editor's lock files, named `.#NAME`, are ignored. A file removed during a scan, as by an editor's save, is indexed at the next refresh.

## Report a reproducible problem

Include the BF version, operating system, the failing command with private paths replaced, its exit code, and expected versus observed behavior. Reproduce it with a small fictional note or record; omit private passages, credentials and logs. Use [GitHub issues](https://github.com/fmind/brain-framework/issues) for bugs and the [private reporting instructions](https://github.com/fmind/brain-framework/blob/main/SECURITY.md) for suspected vulnerabilities.
