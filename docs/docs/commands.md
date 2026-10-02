---
description: Look up Brain Framework commands, previews, graph exports, replies, errors and exit codes.
---

# Command reference

`bf --help` lists the commands from setup to repair; `bf COMMAND --help` (or `-h`) lists a command's arguments, defaults and choices. Help reads no brain and runs no program. Start with [Getting started](getting-started.md) for a first session, and [Troubleshooting](troubleshooting.md) for recovery.

## Commands

| Command                                                                                  | Purpose                                                                                                                                  |
| ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `init PATH [--name NAME] [--full]`                                                       | Create a brain in a new, empty or freshly cloned folder; `--full` adds optional folders.                                                 |
| `register [PATH]`                                                                        | Add a brain's name and path to the optional machine registry.                                                                            |
| `mcp`                                                                                    | Serve `search` and `read` over MCP stdio.                                                                                                |
| `skills DIR [--check] [--force]`                                                         | Install or update the packaged agent skills; see [skills](agents.md#install-the-skills).                                                 |
| `schema [--kind KIND]`                                                                   | Print a configuration or [reply schema](retrieval.md#reply-schemas); defaults to `bf.yaml`.                                              |
| `search QUERY [--scope SCOPE] [--limit N] [--offset N]`                                  | Find words or an identity, optionally within one scope.                                                                                  |
| `read [REF] [--rel RELATION] [--offset N]`                                               | Open home, a page, note, section, record or identity; `--rel` lists one relation's links.                                                |
| `export [--kind edges\|identities]`                                                      | Print the graph of the selected brains as JSON Lines, from their caches.                                                                 |
| `collect SENSOR [--since TIME] [--until TIME] [--dry-run] [--allow-removal]`             | Run one sensor now.                                                                                                                      |
| `run [ROUTINE] [ARGS]... [--hook EVENT] [--dry-run]`                                     | Run one routine, or every routine of a hook, now; see [routines](routines.md).                                                           |
| `update [--sensor NAME] [--routine NAME] [--dry-run]`                                    | Run due sensors, then due routines, then refresh the cache.                                                                              |
| `watch [--interval N] [--poll-interval N] [--notify MODE] [--json]`                      | Run due programs until you quit, with a live dashboard; see [watch](schedule.md).                                                        |
| `schedule [--backend NAME] [--every N] [--name NAME] [--output DIR] [--executable PATH]` | Generate native scheduler files and their installation commands; run nothing.                                                            |
| `status [--check] [--watch]`                                                             | Report caches, records, program health, logs and local usage.                                                                            |
| `validate`                                                                               | Check notes, links, actions, record files and program files.                                                                             |
| `eval [--path evals] [--baseline FILE]`                                                  | Run retrieval cases; compare ranks with a saved reply.                                                                                   |
| `build [--reproject SENSOR [--dry-run]]`                                                 | Recover interrupted record writes and rebuild the search cache; `--reproject` [re-applies mappings](schema.md#reproject-stored-records). |

Every command taking `--brain NAME|PATH` selects brains as [Configuration](configuration.md#select-a-brain) describes. `collect`, `run`, `update`, `watch` and `schedule` act on exactly one brain; `update`, `watch` and `schedule` also accept repeatable `--sensor` and `--routine` [selectors](schedule.md#select-programs).

### Preview an update or a collection

```bash
bf update --dry-run
bf collect local-documents --dry-run
```

`update --dry-run` lists due programs with their windows, names enabled manual programs under `manual`, and runs nothing. `collect --dry-run` **runs** the sensor and returns up to three sample records without saving them or its run; it can contact providers and still appends to the sensor's log. The second example needs the [local-documents sensor](sensors.md#your-first-sensor).

Without time options, `collect` covers the sensor's `lookback` through now. `--since` and `--until` accept `now`, `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD` and ISO 8601 timestamps with a timezone. A snapshot sensor still returns its whole list, and `--allow-removal` accepts [a large removal](sensors.md#define-the-scope-before-adding-a-sensor) once.

### Export the graph

`bf export` prints one JSON object per line for each claim in the selected brains and their direct references, for tools such as DuckDB or networkx. Like search, it reads the caches offline. On the [example brain](https://github.com/fmind/brain-framework/blob/main/examples/brain/README.md#export-the-graph):

```bash
bf export | head -1
```

```json
{
  "brain": "example",
  "origin": "bf://example/actions/2026-09-19_retention/ACTION.md",
  "relation": "tagged-with",
  "subject": "bf://example/actions/2026-09-19_retention/ACTION.md",
  "target": "bf://example/tags/retention"
}
```

Each line has `brain`, `subject`, `relation`, `target` and `origin`, plus the origin's `date` for a note or `time` for a record, and `observed` for a record. `relation` is a declared relation, `cites`, `tagged-with` or `links` for an untyped link. Lines are sorted by subject, relation, target and origin within each brain.

`bf export --kind identities` prints one line for each note or record that answers to more than its own address, such as through an alias, with every name it answers to, so duplicate subjects stand out:

```json
{
  "brain": "example",
  "kind": "note",
  "names": ["bf://example/projects/example", "bf://example/projects/example.md", "repo:example/project"],
  "ref": "projects/example.md",
  "status": "stable",
  "type": "project"
}
```

A busy cache is noted on stderr. A skipped file prints `bf: BRAIN: FILE: ERROR` on stderr and makes the command exit 1 after the other lines.

### Shell completion

Load the completion script for your shell, for example from its startup file:

```bash
eval "$(_BF_COMPLETE=source_bash bf)"   # bash, in ~/.bashrc
eval "$(_BF_COMPLETE=source_zsh bf)"    # zsh, in ~/.zshrc
_BF_COMPLETE=source_fish bf | source    # fish, in ~/.config/fish/config.fish
```

Typing `bf sea` and Tab then completes `search`.

## Exit codes and errors

Results are compact UTF-8 JSON on stdout; help and version are plain text. Dashboards use the full terminal, and `watch --json` streams JSON Lines. Diagnostics go to stderr, prefixed `bf:`, and name files and positions without quoting your content. Control characters in collected text are escaped, such as `\u009b`.

| Code  | Meaning                                                       |
| ----- | ------------------------------------------------------------- |
| `0`   | Success.                                                      |
| `1`   | Operation or check failed, including output to a closed pipe. |
| `2`   | Invalid command-line input.                                   |
| `130` | Cancelled by Ctrl-C, SIGTERM or a closed terminal.            |

Invalid input exits 2 and names the option or argument, before any brain is read when possible. Examples: `bf search "!!!"` (no word to search), `bf search product --limit 0`, `bf read 2026-13` (no such month), `bf read projects/../bf.yaml` and `bf read projects/new-website.md --rel nope`, which lists the valid relations. An option that takes one value may appear once: `bf search launch --scope projects --scope concepts` is invalid instead of searching only the last scope. Only `--sensor` and `--routine` repeat, to select several programs.

A well-formed ref that names nothing exits 1 and suggests what exists: a close page, note, sensor or routine name (`did you mean projects?`), or the note's sections for a missing `#section`.

`update` and `run` exit 1 when a program fails, and `update` and `build` when the refreshed cache skipped files; successful programs keep their results. `status --check` exits 1 on problems, unavailable brains or references, or scheduled programs that failed, are `overdue` or `never` succeeded. `validate` exits 1 on problems, never on warnings.

## Using replies

Replies state a note's day as `date` (`2026-09-27`), as written in its `updated`, and other datetimes with your local offset, to the second (`2026-09-29T09:00:00+02:00`). Values under `attributes` and `fields` are returned exactly as stored. Files, run history and the cache keep UTC. Search and read replies follow the [retrieval reference](retrieval.md); `problems` is always a list of objects with `error` and, when known, `brain` and `file`.

An `update` reply has `ok`, `dry_run`, its `brain`, the `sensors` and `routines` it ran or found due, `manual` for skipped manual programs, and the refreshed `index`. A `run` reply has `ok`, `dry_run`, any `hook` and its `routines`, each with a `status` of `ran`, `skipped` or `failed`.

### Status sources and routines

`bf status` groups results under `brains`. Each brain has its `cache`, `notes`, `problems`, `attention`, `sources`, `routines`, `coverage` totals and `usage`. `healthy` is false when any brain has a problem, an `attention` entry, an unavailable cache or cannot load. A brain that cannot load appears with only `brain`, `error` and, when known, `path`.

| Field                                | Meaning                                                                                                                                                                               |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cache`                              | `ready`, or `busy` while a writer updates it. `stale` (the last cache) or `missing` (none) while an interrupted write awaits recovery, unlike the `stale` of search and read replies. |
| `pending_transaction`                | `true` when `memories/.pending` holds an interrupted write; a problem names the command to recover it.                                                                                |
| `attention`                          | Scheduled programs this machine runs that `failed` or are `overdue` or `never` succeeded, such as `{"sensor":"mail","freshness":"never","failed":true}`, as on the home page.         |
| `state`                              | `active`, `disabled` or `historical` (records remain but `bf.yaml` no longer declares the sensor).                                                                                    |
| `freshness`                          | `fresh`, `overdue`, `never`, `manual` or `unknown`; see [timing and health](schedule.md#timing-and-health).                                                                           |
| `records`, `bytes`                   | Indexed records of the source and their size on disk.                                                                                                                                 |
| `mode`, `window`                     | Sensor mode and, for window sensors, the contiguous collected interval `{since, until}`.                                                                                              |
| `last_collected`                     | The last collection that brought coverage up to date; routines report `last_success`.                                                                                                 |
| `last_run`                           | Counters of the last success: `records`, `added`, `updated`, `unchanged`, `removed`, `requested_start`, `requested_end`, `reconcile`, `elapsed_seconds`, `output_bytes`.              |
| `reconciled`                         | The last scheduled [reconciliation](sensors.md#frequent-updates-and-periodic-reconciliation).                                                                                         |
| `action`                             | A routine's latest action.                                                                                                                                                            |
| `failed`, `error`, `failures`, `log` | The last attempt failed: its diagnostic, consecutive failures and `logs/NAME.log`.                                                                                                    |

For example, an hourly `mail` sensor that collected one message reports:

```json
{
  "state": "active",
  "freshness": "fresh",
  "mode": "window",
  "last_collected": "2026-09-29T15:00:00+02:00",
  "window": { "since": "2026-09-28T15:00:00+02:00", "until": "2026-09-29T15:00:00+02:00" },
  "records": 1,
  "bytes": 612,
  "last_run": { "records": 1, "added": 1, "updated": 0, "unchanged": 0, "removed": 0, "elapsed_seconds": 0.02 }
}
```

A brain also lists `warnings` when a source or authored folder holds more than 80% of the [100,000-entry scan limit](limits.md#size-bounds), such as `{"warning":"directory nears the scan limit","directory":"memories/mail","entries":81234,"limit":100000}`. Warnings never fail `status --check`.

With [jq](https://jqlang.org/), list projects needing review:

```bash
set -o pipefail
bf read projects | jq -r '.items[] | select(.review) | [.ref, .next // ""] | @tsv'
```

Each line holds a project ref and its first open task. `pipefail` keeps a failed `bf` exit status even when `jq` succeeds; follow `next_offset` for brains with more than 200 projects.
