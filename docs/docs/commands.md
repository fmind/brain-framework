---
description: Look up Brain Framework commands, selection rules, previews, output formats and exit codes.
---

# Command reference

Choose a command by what you want to do. Run `bf --help` for command groups and first-use examples, then `bf COMMAND --help` for arguments, options, defaults and accepted choices. Every command also accepts `-h`; help works without a configured brain. Start with [Getting started](getting-started.md) for a complete first session.

```bash
bf -h
bf collect --help
```

Both commands print help and exit 0 without reading a brain or running a sensor. Collection help explains that its dry-run can contact providers.

For recovery steps, use [Troubleshooting](troubleshooting.md). Examples below assume the brain from Getting started unless another prerequisite is named.

## Commands

| Command                                                                      | Purpose                                                                                     |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `build`                                                                      | Recover interrupted record writes and rebuild the disposable search cache.                  |
| `collect SENSOR [--since TIME] [--until TIME] [--dry-run] [--allow-removal]` | Run one sensor now. Dry-run executes it and previews three samples without saving records.  |
| `eval [--path evals]`                                                        | Run retrieval acceptance cases.                                                             |
| `init PATH [--name NAME] [--full]`                                           | Create a brain in a new, empty or freshly cloned directory. `--full` adds optional folders. |
| `mcp`                                                                        | Serve `search` and `read` over MCP stdio.                                                   |
| `read [REF] [--offset N]`                                                    | Open home, a page, note, section, record or identity.                                       |
| `register [PATH]`                                                            | Add a brain's name and path to the optional machine registry.                               |
| `schedule [--backend NAME] [--every N] [--output DIR]`                       | Generate optional native scheduling files and installation commands.                        |
| `schema [--kind brain\|watch\|registry\|eval]`                               | Print an offline editor schema; defaults to `bf.yaml`.                                      |
| `search QUERY [--scope SCOPE] [--limit N] [--offset N]`                      | Find words or an identity, optionally within one scope.                                     |
| `status [--check] [--watch]`                                                 | Report cache, evidence counts, source and routine health, logs and local usage.             |
| `update [--dry-run] [--sensor NAME] [--routine NAME]`                        | Run due sensors, then routines, then refresh the cache. Dry-run executes nothing.           |
| `validate`                                                                   | Check notes, concepts, actions, links and record files.                                     |
| `watch [--interval N] [--poll-interval N] [--notify MODE] [--json]`          | Refresh due information and show a live dashboard; settings live in `settings/watch.yaml`.  |

`watch` runs due programs in a keyboard-driven terminal dashboard; `status --watch` observes local program history without executing anything. Both dashboards [sort in-session](schedule.md#sort-the-dashboard). `schedule` previews native scheduler files and installation commands as JSON; `--output DIR` writes those files without activating them. All three use one brain. See [Watch and schedule updates](schedule.md).

### Select a brain

Run examples inside your brain directory:

```bash
cd ~/brain
bf read
bf search "visitors clear explanation" --scope projects
bf read projects/new-website.md#decision
```

The first command opens home; the other two find and read the website decision from [Getting started](getting-started.md).

Outside a brain, see [brain selection](configuration.md#select-a-brain) for `--brain`, `BF_BRAIN`, registered brains and the one brain that `update`, `collect`, `watch` and `schedule` act on.

### Preview an update or a collection

These previews do different work:

```bash
bf update --dry-run
bf collect local-documents --dry-run
```

`update --dry-run` lists due work and runs nothing. `collect --dry-run` runs the named sensor and returns up to three sample records; it can contact providers and update its private stderr log, but saves neither records nor run history, even when it fails. The second example requires the [local-documents sensor](sensors.md#your-first-sensor).

Without time options, `collect` uses the sensor's lookback through now. For a configured `git-commits` window sensor, choose a period explicitly to backfill:

```bash
bf collect git-commits --since 2026-09-01 --until 2026-10-01
```

Times accept `now`, `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD` and timezone-aware ISO 8601 timestamps. A sensor decides how to apply that window; a [snapshot sensor](sensors.md) still represents a full current catalog. A snapshot that would empty its catalog or remove most of it fails; after checking the sensor's scope, `bf collect SENSOR --allow-removal` accepts that removal for one run. See [snapshot removal](sensors.md#define-the-scope-before-adding-a-sensor).

`update`, `watch` and `schedule` accept repeatable `--sensor NAME` and `--routine NAME` selectors. With neither option, all configured programs are eligible. When either option is present, only the named programs are eligible; omitted kinds are excluded. Unknown names fail before execution. Disabled programs and `refresh: 0` remain excluded from automatic execution. For example, `bf update --sensor git-commits --dry-run` previews only that sensor.

Use `--brain PATH` in scripts and schedules to name the one brain `update` acts on. An `update` of a brain that another update is running waits up to 10 minutes for it to finish, then computes its own due list; see [collector ownership](schedule.md#collector-ownership).

### Shell completion

Complete command and option names by loading the script for your shell, for example from its startup file:

```bash
eval "$(_BF_COMPLETE=source_bash bf)"   # bash, in ~/.bashrc
eval "$(_BF_COMPLETE=source_zsh bf)"    # zsh, in ~/.zshrc
_BF_COMPLETE=source_fish bf | source    # fish, in ~/.config/fish/config.fish
```

Typing `bf sea` and pressing Tab then completes `search`. Completion reads no brain and runs no program.

## Exit codes

Results use compact JSON on stdout; help and version use plain text. The interactive `watch` and `status --watch` commands use a full-screen terminal display; `watch --json` emits JSON Lines snapshots every `poll_interval` (2 seconds by default). Pressing `q` exits a dashboard successfully; Ctrl-C still exits 130. Diagnostics go to stderr and identify files and positions without quoting private content.

| Code  | Meaning                                                                |
| ----- | ---------------------------------------------------------------------- |
| `0`   | Success.                                                               |
| `1`   | Operation or check failed, including output to an already closed pipe. |
| `2`   | Invalid command-line input.                                            |
| `130` | Cancelled by Ctrl-C, SIGTERM or a closed terminal.                     |

Invalid option values and malformed refs name the option or `REF` on stderr before selecting a brain when no brain configuration is needed to validate them. For example, `bf search "product" --limit 0` exits 2 with a `--limit` diagnostic and no JSON result; choose a limit from 1 to 50. Likewise, `bf read 2026-13` (no such month), `bf read bf://` and `bf read projects/../bf.yaml` exit 2, and so does `bf eval --path /etc`, whose path must be brain-relative. A query without any word or identity, such as `bf search "!!!"`, also exits 2, and so does a query shaped like a malformed BF address, such as `bf search bf://Me/x` or `bf search bf://brain` without its trailing slash. Query and scope errors name `QUERY` or `--scope`, such as `Invalid value for --scope: since must be earlier than until` for `--scope 0d`. A well-formed ref that names nothing exits 1. Collection windows require `--since` earlier than `--until`.

Cancellation exits 130 without a message. JSON replies are UTF-8 whatever the terminal's locale; DEL and C1 control characters in collected text, which some terminals obey, are written as JSON escapes such as `\u009b`, which decode to the same value. A note that is not valid UTF-8 fails with its path, for example `bf: projects/latin.md: note is not UTF-8`.

`status --check` fails for unavailable brains, `brains:` references that retrieval cannot include, skipped files, stale caches, or enabled scheduled programs that failed, never succeeded locally, have a future success timestamp or exceeded twice their refresh interval. Use it when a script needs an exit status as well as a health report:

```bash
bf status --check
```

`update` fails if collection, a routine or cache refresh fails; successful programs still retain their results. `build` and `update` also exit 1 when the refreshed cache skipped files: their `skipped` count says how many, and `bf status` or `bf validate` names them.

## Using replies

Search items, page entries and exact reads name their `brain`. Health replies group results under `brains`, with `sources` for collected evidence and `routines` for declared routines. A reference that retrieval cannot include appears in its declaring brain's `problems` with `file: bf.yaml`. A brain that cannot be loaded, or a registered one absent on this machine, is an entry with only `brain`, its registered or directory name, `error` and, when present, `path`. An `update` reply is one object with `ok`, `dry_run`, its `brain`, the `sensors` and `routines` lists, empty when nothing ran, and the refreshed `index`, absent on a dry run; a cycle that cannot start, such as one still waiting for another update after 10 minutes, fails with a diagnostic instead. Record refs remain `source:id`. Every reply instant is UTC ISO 8601 with microseconds and a `Z` suffix, such as `2026-09-27T12:00:00.000000Z`, so instants compare correctly as strings.

### Status sources and routines

`bf status` describes each source with the same coverage fields as search `sources` and read `collection`, then adds this machine's totals and run details. Fields without a value are omitted.

| Field                                | Meaning                                                                                                                                                                                |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `state`                              | `active`, `disabled` or `historical` (records remain but `bf.yaml` no longer declares the sensor). Routines: `active` or `disabled`.                                                   |
| `freshness`                          | `fresh`, `stale`, `never`, `manual` or `unknown`; see [timing and health](schedule.md#timing-and-health).                                                                              |
| `mode`, `window`                     | Sensor mode and, for window sensors, the contiguous collected interval `{since, until}`.                                                                                               |
| `last_collected`                     | Last collection that brought coverage up to its run; backfills leave it unchanged. Routines report `last_success`.                                                                     |
| `records`, `latest`                  | Indexed record total and latest record time for the source.                                                                                                                            |
| `last_run`                           | Counters of the last successful collection: `records`, `added`, `updated`, `unchanged`, `removed`, `requested_start`, `requested_end`, `reconcile`, `elapsed_seconds`, `output_bytes`. |
| `reconciled`                         | Last successful scheduled [reconciliation](sensors.md#frequent-updates-and-periodic-reconciliation).                                                                                   |
| `action`                             | A routine's latest action ref.                                                                                                                                                         |
| `failed`, `error`, `failures`, `log` | Present when the last attempt failed: the diagnostic, consecutive failures and the private stderr log.                                                                                 |
| `stale`                              | For enabled scheduled programs: whether freshness is `never` or `stale`.                                                                                                               |

For example, a fictional hourly `mail` sensor that collected one message over the last day reports this entry under `sources.mail`; the elapsed time varies:

```json
{
  "state": "active",
  "freshness": "fresh",
  "last_collected": "2026-09-23T13:00:00.000000Z",
  "mode": "window",
  "window": { "since": "2026-09-22T13:00:00.000000Z", "until": "2026-09-23T13:00:00.000000Z" },
  "records": 1,
  "latest": "2026-09-23T09:30:00.000000Z",
  "last_run": {
    "records": 1,
    "added": 1,
    "updated": 0,
    "unchanged": 0,
    "removed": 0,
    "requested_start": "2026-09-22T13:00:00.000000Z",
    "requested_end": "2026-09-23T13:00:00.000000Z",
    "reconcile": false,
    "elapsed_seconds": 0.02,
    "output_bytes": 68
  },
  "stale": false
}
```

A failed scheduled program instead carries `"failed":true`, its `error`, `failures` and `log`, and makes `bf status --check` exit 1.

Inspect `problems`, `stale` and source coverage before treating an answer as complete. `problems` is always a list of objects with `error` and, when known, `brain` and `file`. Status reports each brain's `cache` state as `ready` or `stale`. Follow returned `next_offset` values and assemble large exact-read chunks as described in [continuations](retrieval.md#continuations). With several selected brains, read each result's `uri`; see [identity matching](retrieval.md#identity-matching).

With [jq](https://jqlang.org/), list projects needing review:

```bash
set -o pipefail
bf read projects |
  jq -r '.items[] | select(.review) | [.ref, .next // ""] | @tsv'
```

Each output line contains a project ref and its first open task, if any. No lines means this page has no projects marked for review; check completeness and continue through any remaining pages before drawing a conclusion about the whole brain. `pipefail` preserves a failed Brain Framework command's exit status even when the formatter succeeds.
