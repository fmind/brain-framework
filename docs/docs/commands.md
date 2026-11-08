---
icon: lucide/square-terminal
description: Look up Brain Framework commands, previews, graph exports, replies, errors and exit codes.
---

# Command reference

`bf --help` lists the commands from setup to repair. `bf COMMAND --help` (or `-h`) lists a command's arguments, defaults and choices. Help reads no brain, runs no program and answers even when other values on the line are invalid. Start with [Getting started](getting-started.md) for a first session, and [Troubleshooting](troubleshooting.md) for recovery.

## Commands

| Command                                                                                     | Purpose                                                                                                                                  |
| ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `init PATH [--name NAME] [--full]`                                                          | Create a brain in a new, empty or freshly cloned folder; `--full` adds optional folders.                                                 |
| `register [PATH]`                                                                           | Add or move a brain's entry in the optional [machine registry](configuration.md#optional-machine-registration).                          |
| `mcp`                                                                                       | Serve `search` and `read` over MCP stdio; needs the [`mcp` extra](mcp.md#before-connecting).                                             |
| `skills DIR [--check] [--force]`                                                            | Install or update the packaged agent skills; see [skills](agents.md#install-the-skills).                                                 |
| `schema [--kind KIND]`                                                                      | Print a configuration or [reply schema](retrieval.md#reply-schemas); defaults to `bf.yaml`.                                              |
| `search QUERY [--scope SCOPE] [--limit N] [--offset N]`                                     | Find words or an identity, optionally within one scope, which a period can [narrow](retrieval.md#scopes).                                |
| `read [REF] [--rel RELATION] [--offset N]`                                                  | Open home, a page, note, section, record or identity; `--rel` lists one relation's links.                                                |
| `export [--kind edges\|identities\|items] [--scope SCOPE]`                                  | Print the graph, or every note and record with its text, as JSON Lines from the caches, within an optional [scope](retrieval.md#scopes). |
| `collect SENSOR [--since TIME] [--until TIME] [--dry-run] [--allow-removal] [--input FILE]` | Run one sensor now, or [check its saved output](sensors.md#check-saved-output) without running it.                                       |
| `forget REF... [--dry-run]`                                                                 | Remove collected records and keep their sensors from storing them again; see [forget records](#forget-records).                          |
| `run [PROGRAM] [ARGS]... [--hook EVENT] [--stdin] [--dry-run]`                              | Run one routine or [assistant](prompts.md), or every routine of a hook, now; see [routines](routines.md).                                |
| `channel read CHANNEL --since START [--until END]`                                          | Read one complete batch without changing input progress or invoking an assistant.                                                        |
| `channel send CHANNEL --stdin`                                                              | Save and send one outgoing JSON message through its configured endpoint.                                                                 |
| `queue [ID] [--retry\|--cancel] [--limit N]`                                                | List durable jobs and deliveries, inspect one in private detail or explicitly retry or cancel it; delivery retry sends immediately.      |
| `update [--sensor NAME] [--routine NAME] [--assistant NAME] [--kind KIND] [--dry-run]`      | Run due sensors, then due routines, then due assistant work, then refresh the cache.                                                     |
| `watch [--interval N] [--poll-interval N] [--notify MODE] [--json]`                         | Run due programs until you quit, with a live dashboard; see [watch](schedule.md).                                                        |
| `status [--check] [--watch]`                                                                | Report caches, records, program health, logs and local usage.                                                                            |
| `validate`                                                                                  | Check notes, links, actions, record files and program files.                                                                             |
| `eval [--path evals] [--baseline FILE]`                                                     | Run retrieval cases; compare ranks with a saved reply.                                                                                   |
| `build [--reproject SENSOR [--dry-run]]`                                                    | Recover interrupted record writes and rebuild the search cache; `--reproject` [re-applies mappings](schema.md#reproject-stored-records). |

Every command taking `--brain NAME|PATH` selects brains as [Configuration](configuration.md#select-a-brain) describes:

- `collect`, `run`, `channel`, `queue`, `update`, `watch`, `forget`, `build`, `validate`, `eval` and `status --watch` act on exactly one brain.
- `update` and `watch` also accept repeatable `--sensor`, `--routine` and `--assistant` [selectors](schedule.md#select-programs).

### Preview an update or a collection

```bash
bf update --dry-run
bf collect local-documents --dry-run
```

The second example needs the [local-documents sensor](sensors.md#your-first-sensor). Only the update preview runs nothing:

| Preview             | Runs                           | Result                                                                             |
| ------------------- | ------------------------------ | ---------------------------------------------------------------------------------- |
| `update --dry-run`  | Nothing.                       | Due programs with their windows, and enabled manual programs under `manual`.       |
| `collect --dry-run` | **The sensor.**                | Up to three sample records, without saving them or its run.                        |
| `run --dry-run`     | **Its routines or assistant.** | No action, record or run is written; an assistant's reply still returns as `text`. |

`collect --dry-run` and `run --dry-run` can contact providers and still append to the program's log.

Collection windows:

- Without time options, `collect` covers the sensor's `lookback` through now.
- `--since` and `--until` accept `now`, `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD` and ISO 8601 timestamps with a timezone.
- A snapshot sensor still returns its whole list.
- `--allow-removal` accepts [a large removal](sensors.md#define-the-scope-before-adding-a-sensor) once.

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

Each line has:

- `brain`, `subject`, `relation`, `target` and `origin`;
- the origin's `date` for a note, or its `time` and `observed` for a record.

`relation` is a declared relation, `cites`, `tagged-with`, or `links` for an untyped link. Lines are sorted by subject, relation, target and origin within each brain.

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

`bf export --kind items` prints one line for each note and record, sorted by ref, with its metadata, its [single-value `fields`](schema.md#types-and-cardinality) and its `text`: a note's whole Markdown file or a record's text. A record's metadata includes its upstream `updated` time, when its sensor records one in `attributes.updated`, and the time BF [observed](schema.md#record-revisions-and-provenance) it. It feeds retrievers BF leaves out, such as embedding or multilingual search, which then return refs for `bf read`:

```json
{
  "brain": "example",
  "kind": "record",
  "ref": "mail:m1",
  "source": "mail",
  "title": "Launch moved",
  "time": "2026-09-29T14:20:00+02:00",
  "updated": "2026-09-29T15:05:00+02:00",
  "observed": "2026-09-29T15:10:00+02:00",
  "fields": { "status": "open" },
  "text": "The launch moves to Monday."
}
```

`--scope SCOPE` keeps only the items `bf search --scope SCOPE` would search, with the same [scopes](retrieval.md#scopes) and errors: an invalid scope exits 2 and an unknown source exits 1. Scripts and routines that need every record of one source, period or folder export them all at once, with no page to follow, instead of reading `memories/` files. The [example brain](https://github.com/fmind/brain-framework/blob/main/examples/brain/README.md#export-the-graph) prints its one demo record:

```bash
bf export --kind items --scope memories/demo
```

With `--kind edges`, a scope keeps the claims its items assert; with `--kind identities`, the names its items answer to.

A record its sensor kept only part of adds `"partial":true`, as in [search](retrieval.md#search). A file that changed or became unreadable since the cache refreshed keeps its line without `text` and prints `bf: BRAIN: FILE: text omitted: …` on stderr; run `bf build` first for a complete export. The text is your evidence: send it only to services your [privacy](privacy.md) choices allow.

A busy cache is noted on stderr. A skipped file prints `bf: BRAIN: FILE: ERROR` on stderr and makes the command exit 1 after the other lines.

### Forget records

```bash
bf forget mail:1a167922 memories/mail/9f2c...json --dry-run
bf forget mail:1a167922
```

Each `REF` is a `SOURCE:ID` record ref or a `memories/SOURCE/SHA256.json` path, as `bf read` returns them. The command removes each record in one transaction per source, adds its file's SHA-256 to `memories/SOURCE/.forgotten` and refreshes the cache. Collection then skips that record whenever its sensor prints it, so a snapshot does not bring it back. A ref not collected yet is listed too.

The reply has `ok`, `dry_run`, the `records` with each `ref`, whether it was `removed` now and whether it was `listed` already, and the refreshed `index`. An invalid ref exits 2. `forget` acts on one brain and runs no program. See [keep, forget and expire records](sensors.md#keep-forget-and-expire-records) for Git history and retention.

### Shell completion

Load the completion script for your shell, for example from its startup file:

```bash
eval "$(_BF_COMPLETE=source_bash bf)"   # bash, in ~/.bashrc
eval "$(_BF_COMPLETE=source_zsh bf)"    # zsh, in ~/.zshrc
_BF_COMPLETE=source_fish bf | source    # fish, in ~/.config/fish/config.fish
```

Typing `bf sea` and Tab then completes `search`.

## Exit codes and errors

| Output        | Format                                                                                              |
| ------------- | --------------------------------------------------------------------------------------------------- |
| Results       | Compact UTF-8 JSON on stdout.                                                                       |
| Help, version | Plain text.                                                                                         |
| Dashboards    | The full terminal; `watch --json` streams JSON Lines instead.                                       |
| Diagnostics   | One stderr line each, starting with `bf:`, naming files and positions without quoting your content. |

Collected text and file names are data. Results, `export` lines, `watch --json` snapshots and MCP text escape these characters as JSON `\uXXXX` escapes, with a surrogate pair beyond U+FFFF:

- DEL and C1 controls;
- the line and paragraph separators U+2028 and U+2029, which many readers treat as line breaks;
- Unicode format characters, such as soft hyphens and zero-width, bidi or tag characters.

Decoded values are unchanged. Diagnostics and MCP errors also escape the other control characters, including newlines, so a file name cannot forge another `bf:` line.

| Code  | Meaning                                                                                                    |
| ----- | ---------------------------------------------------------------------------------------------------------- |
| `0`   | Success.                                                                                                   |
| `1`   | Operation or check failed, including standard output that is closed, full or stops reading before the end. |
| `2`   | Invalid command-line input.                                                                                |
| `130` | Cancelled by Ctrl-C, SIGTERM or a closed terminal.                                                         |

Invalid input exits 2 with one line naming the argument, the reason and the help to read. When possible, BF reports it before reading any brain:

```text
bf: invalid input: --limit: 0 is not in the range 1<=x<=50 (see bf search -h)
```

Other invalid inputs:

- `bf search "!!!"`: no word to search.
- `bf read 2026-13`: no such month.
- `bf read projects/../bf.yaml`.
- `bf read tasks#next`: pages have no sections.
- `bf read projects --rel cites`: a page lists no relation.
- `bf read projects/new-website.md --rel nope`: the error lists the valid relations.
- An empty value choosing where or on what a command acts, such as `--brain ""`, `bf init ""`, `bf collect ""`, `run --hook ""`, `--sensor ""`, `collect SENSOR --since ""`, `collect SENSOR --input ""`, `build --reproject ""`.
- A repeated option that takes one value: `bf search launch --scope projects --scope concepts` is invalid instead of searching only the last scope. Only the program selectors repeat: `--sensor`, `--routine`, `--assistant` and `update --kind`.

A well-formed ref that names nothing exits 1 and suggests what exists:

- a close page, note, sensor, routine or assistant name (`did you mean projects?`);
- the note's sections for a missing `#section`.

`collect` and `run` suggest only enabled programs. They fail on a disabled one with `sensor NAME is disabled in bf.yaml; set enabled: true to run it`.

Commands also exit 1 in these cases:

| Command                     | Exits 1 when                                                                                                                  |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `update`, `run`             | A program fails; successful programs keep their results.                                                                      |
| `update`, `build`, `forget` | The refreshed cache skipped files.                                                                                            |
| `status --check`            | `healthy` is false: on problems, an unavailable cache, brain or reference, and every [`attention`](#attention-entries) entry. |
| `eval`                      | A case fails; a lower rank alone does not fail it ([checks](checks.md)).                                                      |
| `export`                    | It skipped a file.                                                                                                            |
| `channel send`              | The delivery's state is not `delivered`.                                                                                      |
| `queue ID --retry`          | A retried channel delivery is still not `delivered`.                                                                          |
| `validate`                  | It finds problems; warnings never fail it.                                                                                    |
| `skills`                    | It keeps a folder holding your edits, foreign files or a newer bf's copy; see [skills](agents.md#install-the-skills).         |
| `skills --check`            | Any skill is not current.                                                                                                     |

## Using replies

Every reply states times with your local offset. Dates and field values follow the [retrieval reference](retrieval.md). Files, run history and the cache keep UTC. Search and read replies follow that reference. `problems` is always a list of objects with `error` and, when known, `brain` and `file`.

An `update` reply has:

- `ok`, `dry_run` and its `brain`;
- the `sensors`, `routines` and, when the brain declares any, `assistants` it ran or found due;
- `manual` for skipped manual programs;
- the refreshed `index`.

Each `assistants` row has:

- `assistant`, its name;
- `status`: `ran`, `due`, `skipped` or `failed`, with its `skipped` reason or `error`;
- what it concerns: a `job` and the `record` it wrote, a `schedule` with its `start` and `end` on a dry run, an `input` read, such as `channel C: read failed: REASON; see logs/channel.C.read.log`, or a `delivery`.

A job waiting for queue space and a delivery still pending are `failed` rows until they finish.

A `run` reply has `ok`, `dry_run`, any `hook` and its `routines`, each with a `status` of `ran`, `skipped` or `failed`. An assistant's `run` reply has `assistants` instead: one entry with its `status` (`ran` or `failed`), the `record` it wrote (none on a dry run) and the reply `text`, or an `error`.

### Program and source status {#status-sources-and-routines}

`bf status` groups results under `brains`. Each brain has:

- its `cache`, `notes`, `problems`, `attention`, `sources`, `coverage` totals and `usage`;
- `routines` and [`assistants`](prompts.md#schedule-and-overlap) when it declares any;
- `messaging` once its assistant queue exists.

`healthy` is false exactly when a brain has a problem or an `attention` entry, its cache is `stale` or `missing`, or a brain or reference is unavailable or cannot load. A brain that cannot load appears with only `brain`, `error` and, when known, `path`.

| Field                                | Meaning                                                                                                                                                                                                                                                           |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cache`                              | `ready`; `busy` while a writer updates it; `stale` (the last cache) or `missing` (none) while an interrupted write awaits recovery, unlike the `stale` of search and read replies.                                                                                |
| `pending_transaction`                | `true` when `memories/.pending` holds an interrupted write; a problem names the command to recover it.                                                                                                                                                            |
| `runtime`                            | The `installed` bf version and, when the brain pins a runtime, the `pinned` one, read as data from the brain's `uv.lock`. Compare their major versions before trusting either.                                                                                    |
| `attention`                          | What needs action; see [attention entries](#attention-entries) below.                                                                                                                                                                                             |
| `state`                              | As in [source coverage](retrieval.md#source-coverage); a routine or assistant is `active` or `disabled`.                                                                                                                                                          |
| `freshness`                          | `fresh`, `overdue`, `never`, `manual` or `unknown`; see [timing and health](schedule.md#timing-and-health).                                                                                                                                                       |
| `records`, `bytes`, `latest`         | Indexed records of the source, their size on disk and the newest event time.                                                                                                                                                                                      |
| `changed`, `silent`                  | When this brain last gained a new or changed record of the source, and `true` when that is older than its [`silent_after`](schedule.md#silent-sources).                                                                                                           |
| `mode`, `window`                     | Sensor mode and, for window sensors, the contiguous collected interval `{since, until}`.                                                                                                                                                                          |
| `last_collected`                     | The last collection that brought the source up to date, which freshness counts from; a [backfill](sensors.md#backfills-and-coverage) never does.                                                                                                                  |
| `last_success`                       | The last successful run of a routine or assistant, reported instead of `last_collected`.                                                                                                                                                                          |
| `last_run`                           | Counters of the last committed run: `records`, `added`, `updated`, `unchanged`, `removed`, `forgotten` and `expired` (when nonzero), `requested_start`, `requested_end`, `reconcile`, `elapsed_seconds`, `output_bytes`.                                          |
| `reconciled`                         | The last scheduled [reconciliation](sensors.md#frequent-updates-and-periodic-reconciliation).                                                                                                                                                                     |
| `action`                             | A routine's latest action.                                                                                                                                                                                                                                        |
| `runs`, `record`                     | An assistant's successful runs and the record its latest run wrote.                                                                                                                                                                                               |
| `schedules`                          | Each named timer of an assistant: its interval, next due time, last success and freshness.                                                                                                                                                                        |
| `failed`, `error`, `failures`, `log` | The last attempt failed: its diagnostic, consecutive failures and `logs/NAME.log`; never on a disabled program.                                                                                                                                                   |
| `messaging`                          | The [assistant queue](channels.md#queue-and-recovery), as `bf queue` lists it, without message bodies; see [messaging](#messaging) below.                                                                                                                         |
| `usage`                              | `7d` (today and the 6 UTC days before) and `30d` counts of `search`, `empty` (no result) and `read`; [`kinds`](limits.md#processes-and-logs) per operation, `empty_kinds` and the top 10 `empty_terms`; `since` when an older release's rotation cut that window. |

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
  "latest": "2026-09-29T14:20:00+02:00",
  "changed": "2026-09-29T15:00:00+02:00",
  "last_run": {
    "records": 1,
    "added": 1,
    "updated": 0,
    "unchanged": 0,
    "removed": 0,
    "requested_start": "2026-09-28T15:00:00+02:00",
    "requested_end": "2026-09-29T15:00:00+02:00",
    "reconcile": false,
    "elapsed_seconds": 0.02,
    "output_bytes": 597
  }
}
```

A brain also lists `warnings` when a source or authored folder holds more than 80% of the [100,000-entry scan limit](limits.md#size-bounds). Warnings never fail `status --check`. For example:

`{"warning":"directory nears the scan limit","directory":"memories/mail","entries":81234,"limit":100000}`

#### Attention entries

Like the home page, `attention` lists:

- scheduled programs this machine runs that `failed`, are `overdue` or `never` succeeded, or are [`silent`](schedule.md#silent-sources), such as `{"sensor":"mail","freshness":"never","failed":true}`;
- assistants with failed or uncertain jobs, whatever their schedules.

Status also lists:

- assistants whose schedules cannot start;
- channels with an unresolved delivery that failed or is uncertain, or with a current stream or read error, such as `{"channel":"chat","freshness":"unknown","failed":true}`.

An assistant also lists its channel `inputs`. It is `failed`, without `failures`, while one of its jobs is failed or uncertain, and so is its source in `sources`.

A disabled sensor, routine or assistant reports no failure and appears in no `attention` entry, so it never fails `status --check` itself. A failed delivery of its answers still fails its channel. Its log keeps the last failure, and `messaging` its unresolved jobs.

#### Messaging

`messaging` holds:

- `jobs`: the latest 50, each with its state and error;
- `deliveries`: the next 50 unresolved, each with its state and error;
- `counts` and `delivery_counts` by state;
- `delivery_failures`: the unresolved deliveries holding an error;
- `transports`: each `health:` entry now reporting an `error`, with its time `at`;
- `wakes`: each assistant's requested wake with its `due` time and the `job` running it, or `null`.

A `transports` entry names a stream (`health:channel:NAME`), a read input (`health:read:ASSISTANT:CHANNEL`), an assistant's schedule admission (`health:assistant:NAME`), the dispatcher (`health:dispatcher`) or a worker lane. Watch clears these entries whenever it starts or stops. Each `bf update` replaces its read and admission entries.

Status and retrieval read the queue without creating or locking it. When status cannot read the queue, it adds a `problems` entry instead of `messaging`. Its assistants then show `unknown` freshness with that `error`, and schedules without `next_due`. Examples of a queue that status cannot read:

- one holding unresolved work under a former path or name;
- one from an earlier release or another build;
- one holding an interrupted write.

#### List projects with jq

With [jq](https://jqlang.org/), list projects needing review:

```bash
(
  set -e
  offset=0
  while [ -n "$offset" ]; do
    page=$(bf read projects --offset "$offset")
    jq -r '.items[] | select(.review) | [.ref, .next // ""] | @tsv' <<<"$page"
    offset=$(jq -r '.next_offset // empty' <<<"$page")
  done
)
```

Each line holds a project ref and its first open task. Pages end near 32 KiB, often after a few dozen projects, so the loop follows `next_offset` until it is absent. The subshell stops at the first failed command and keeps its exit status without closing your shell.
