# Command reference

Choose a command by what you want to do. Run `bf COMMAND --help` for every option; start with [Getting started](getting-started.md) for a complete first session.

## Commands

| Command                                                             | Purpose                                                                                     |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `build`                                                             | Recover interrupted record writes and rebuild the disposable search cache.                  |
| `collect SENSOR [--since TIME] [--until TIME] [--dry-run]`          | Run one sensor now. Dry-run executes it and previews three samples without saving records.  |
| `eval [--path evals]`                                               | Run retrieval acceptance cases.                                                             |
| `init PATH [--name NAME] [--full]`                                  | Create a brain in a new, empty or freshly cloned directory. `--full` adds optional folders. |
| `mcp`                                                               | Serve `search` and `read` over MCP stdio.                                                   |
| `read [REF] [--offset N]`                                           | Open home, a page, note, section, record or identity.                                       |
| `register [PATH]`                                                   | Add a brain's name and path to the optional machine registry.                               |
| `schedule [--backend NAME] [--every N] [--output DIR]`              | Generate optional native scheduling files and installation commands.                        |
| `schema`                                                            | Print the JSON Schema for `bf.yaml`.                                                        |
| `search QUERY [--scope SCOPE] [--limit N] [--offset N]`             | Find words or an identity, optionally within one scope.                                     |
| `status [--check] [--watch]`                                        | Report cache, evidence counts, source and routine health, logs and local usage.             |
| `update [--dry-run] [--sensor NAME] [--routine NAME]`               | Run due sensors, then routines, then refresh the cache. Dry-run executes nothing.           |
| `validate`                                                          | Check notes, concepts, actions, links and record files.                                     |
| `watch [--interval N] [--poll-interval N] [--notify MODE] [--json]` | Refresh due information and show a live dashboard; settings live in `settings/watch.yaml`.  |

`watch` runs due programs in a keyboard-driven terminal dashboard; `status --watch` observes local program history without executing anything. `schedule` previews native scheduler files and installation commands as JSON; `--output DIR` writes those files without activating them. All three use one brain. See [Watch and schedule updates](schedule.md).

### Select a brain

Run examples inside your brain directory:

```bash
cd ~/brain
bf read
bf search "visitors clear explanation" --scope projects
bf read projects/new-website.md#decision
```

The first command opens home; the other two find and read the website decision from [Getting started](getting-started.md).

Existing-brain commands select roots in this order: `--brain NAME|PATH`, then `BF_BRAIN`, then the enclosing brain, then all registered brains. Commands needing one root ask for an explicit selection when several are registered. Registration is optional; see [configuration](configuration.md).

### Preview an update or a collection

These previews do different work:

```bash
bf update --dry-run
bf collect local-documents --dry-run
```

`update --dry-run` lists due work and runs nothing. `collect --dry-run` runs the named sensor and returns up to three sample records; it can contact providers and update its private stderr log, but saves neither records nor success history. The second example requires the [local-documents sensor](sensors.md#your-first-sensor).

Without time options, `collect` uses the sensor's lookback through now. For a configured `git-commits` window sensor, choose a period explicitly to backfill:

```bash
bf collect git-commits --since 2026-09-01 --until 2026-10-01
```

Times accept `now`, `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD` and timezone-aware ISO 8601 timestamps. A sensor decides how to apply that window; a [snapshot sensor](sensors.md) still represents a full current catalog.

`update`, `watch` and `schedule` accept repeatable `--sensor NAME` and `--routine NAME` selectors. With neither option, all configured programs are eligible. When either option is present, only the named programs are eligible; omitted kinds are excluded. Unknown names fail before execution. Disabled programs and `refresh: 0` remain excluded from automatic execution. For example, `bf update --sensor git-commits --dry-run` previews only that sensor.

`update` acts only on selected roots, never their references. Use `--brain PATH` in scripts and schedules to make that selection explicit.

## Exit codes

Results use compact JSON on stdout; help and version use plain text. The interactive `watch` and `status --watch` commands use a full-screen terminal display; `watch --json` emits JSON Lines snapshots at the configured polling interval (two seconds by default). Pressing `q` exits a dashboard successfully; Ctrl-C still exits 130. Diagnostics go to stderr and identify files and positions without quoting private content.

| Code  | Meaning                                            |
| ----- | -------------------------------------------------- |
| `0`   | Success.                                           |
| `1`   | Operation or check failed.                         |
| `2`   | Invalid command-line input.                        |
| `130` | Cancelled by Ctrl-C, SIGTERM or a closed terminal. |

`status --check` fails for unavailable brains, skipped files, stale caches, or enabled scheduled programs that failed, never succeeded locally or exceeded twice their refresh interval. Use it when a script needs an exit status as well as a health report:

```bash
bf status --check
```

`update` fails if collection, a routine or cache refresh fails; successful programs still retain their results.

## Using replies

Search items, page entries and exact reads name their `brain`. Health and update replies group results under `brains`. Health uses `sources` for collected evidence and `routines` for declared routines; execution results use `sensors` and `routines`. Record refs remain `source:id`.

Inspect `problems`, `stale` and source coverage before treating an answer as complete. Follow returned `next_offset` values and assemble large exact-read chunks as described in [continuations](retrieval.md#continuations).

With [jq](https://jqlang.org/), list projects needing review:

```bash
set -o pipefail
bf read projects |
  jq -r '.items[] | select(.review) | [.ref, .next // ""] | @tsv'
```

Each output line contains a project ref and its first open task, if any. No lines means this page has no projects marked for review; check completeness and continue through any remaining pages before drawing a conclusion about the whole brain. `pipefail` preserves a failed Brain Framework command's exit status even when the formatter succeeds.
