---
icon: lucide/square-terminal
description: Look up Brain Framework commands, previews, graph exports, replies, errors and exit codes.
---

# Command reference

`bf --help` lists every command; `bf COMMAND -h` lists arguments, defaults and choices without reading a brain or running programs. Boolean flags are positive-only (`--check`, `--force`, `--dry-run`, `--retry`, `--cancel`, `--watch`, `--link`, `--allow-removal`, `--stdin`, `--json`, `--full`).

## Commands

| Command                                                                                  | Purpose                                                                                                                              |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `init PATH [--name NAME] [--full]`                                                       | Create a brain in a new, empty or freshly cloned folder; `--full` adds optional folders.                                             |
| `register [PATH] [--name NAME] [--default]`                                              | Add or update a brain's entry in the optional [machine registry](configuration.md#related-brains).                                   |
| `unregister TARGET`                                                                      | Remove a brain entry from the optional [machine registry](configuration.md#related-brains).                                          |
| `mcp`                                                                                    | Serve read-only `search` and `read` over MCP stdio; needs the [`mcp` extra](mcp.md#before-connecting).                               |
| `guide [TOPIC]`                                                                          | List packaged user guides (`bf guide`) or print one guide topic.                                                                     |
| `skill install [--brain BRAIN] [--global] [--check] [--force]`                           | Install, verify (`--check`), or overwrite (`--force`) the packaged `/bf` skill in project or global agent folders.                   |
| `action new SLUG [--title TITLE] [--summary SUMMARY] [--unique]`                         | Create a validated `status: draft` `actions/YYYY-MM-DD_SLUG/ACTION.md` note (`--unique` adds a 12-hex suffix) and refresh the cache. |
| `action status SLUG draft\|stable\|deprecated`                                           | Update an action's OKF `status` atomically (`draft` = open on Home, `stable` = settled, `deprecated` = cancelled).                   |
| `schema [--kind KIND]`                                                                   | Print a configuration or [reply schema](retrieval.md#reply-schemas) (`bf.yaml` by default).                                          |
| `search QUERY [--scope SCOPE] [--limit N] [--offset N]`                                  | Search words or an identity, optionally within one [scope](retrieval.md#scopes).                                                     |
| `read [REF] [--rel RELATION] [--offset N]`                                               | Open Home, `review`, another page, note, `#section`, record or identity; `--rel` lists incoming links of one relation.               |
| `export [--kind edges\|identities\|items] [--scope SCOPE] [--observed-since TIME]`       | Stream graph edges, multi-name identities or full items as JSON Lines from the caches.                                               |
| `collect JOB [--since TIME] [--until TIME] [--dry-run] [--allow-removal] [--input FILE]` | Run one record job now, or [validate saved JSON/JSONL output](sensors.md#check-saved-output) without running it.                     |
| `forget REF... [--dry-run]`                                                              | Remove collected records and block future collection via `.forgotten`.                                                               |
| `run [JOB] [ARGS]... [--hook EVENT] [--stdin] [--dry-run] [--since TIME] [--until TIME]` | Run one configured job (`run`, `prompt`, `send`) or every job matching `--hook EVENT`.                                               |
| `wake [--job JOB] [--in DURATION\|--at ISO] [--reason TEXT] [--run ID] [--key K]`        | Queue a bounded follow-up wake for an enabled job.                                                                                   |
| `post [TEXT] [--to JOB] [--thread ID] [--target ID] [--reply-to ID] [--stdin] [--key K]` | Queue and deliver one outbound message through a `send:` job.                                                                        |
| `queue [ID] [--retry\|--cancel] [--limit N]`                                             | List runs, deliveries and wakes, open one item in detail, or retry/cancel it.                                                        |
| `update [--job NAME] [--dry-run]`                                                        | Run due record jobs, scheduled/event jobs, wakes and deliveries, then refresh the cache.                                             |
| `watch [--job NAME] [--interval N] [--poll-interval N] [--notify MODE] [--json]`         | Continuously run `daemon` jobs, due jobs, events, wakes and deliveries ([watch](schedule.md)).                                       |
| `status [--check] [--watch]`                                                             | Report caches, record counts, job health, logs, queue summary and local usage.                                                       |
| `doctor`                                                                                 | Check configured job executables, required binaries, declared `requires.env` presence, scripts and prompts.                          |
| `validate`                                                                               | Check notes, links, identities, actions, records and job files.                                                                      |
| `eval [--path evals] [--baseline FILE]`                                                  | Run retrieval test cases and compare ranks against an optional baseline.                                                             |
| `build [--reproject JOB [--dry-run]]`                                                    | Recover interrupted writes and rebuild `.bf/`; `--reproject` [re-applies field mappings](schema.md#reproject-stored-records).        |

Brain selection ([Configuration](configuration.md#select-a-brain)):

- Commands act on one selected brain (`--brain`, `BF_BRAIN`, enclosing `bf.yaml`, or registry `default`).

### Preview an update or a collection

```bash
bf update --dry-run
bf collect local-documents --dry-run
```

| Preview             | Executes     | Result                                                                            |
| ------------------- | ------------ | --------------------------------------------------------------------------------- |
| `update --dry-run`  | Nothing.     | Lists due jobs with windows, plus enabled manual jobs under `manual`.             |
| `collect --dry-run` | **The job.** | Previews counts and up to three sample records without saving records or history. |
| `run --dry-run`     | **The job.** | Writes no action or delivery; returns extracted text (`text`) when produced.      |

`--since`, `--until` and `--observed-since` accept `now`, `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD` and timezone-aware ISO 8601 instants. `collect` accepts a JSON array, JSON Lines (one JSON object per line) or `{records, incomplete}` on sensor stdout and `--input FILE` (`-` for stdin).

### Export the graph

`bf export` reads the validated cache offline across the selected brain, emitting one JSON object per line:

- **`--kind edges` (default)**: `brain`, `subject`, `relation` (any relation, `cites`, `tagged-with` or `links`), `target`, `origin`, plus `date` (notes) or `time`/`observed` (records), sorted by subject, relation, target and origin.
- **`--kind identities`**: every note or record answering to more than its own file/ref address (`brain`, `kind`, `names`, `ref`, `status`, `type`).
- **`--kind items`**: every note and record sorted by `ref`, carrying metadata, optional `@type`, `relations` (`dict[str, list[str]]`), stored record `attributes` (`dict[str, Any]`) and full `text` (plus `"partial": true` when truncated):

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
  "relations": { "participant": ["person:email/alice@example.test"] },
  "attributes": { "observed": "2026-09-29T13:10:00Z", "status": "open" },
  "text": "The launch moves to Monday."
}
```

- **`--scope SCOPE`**: restricts output to items matching [`bf search --scope SCOPE`](retrieval.md#scopes).
- **`--observed-since TIME`** (`--kind items`): keeps collected records (`kind: "record"`) whose `observed` timestamp is at or after the cutoff, so gates and external indexers read incremental batches without scanning older items.

Changed/unreadable files since the last cache refresh omit `text` with `bf: FILE: text omitted: …` on stderr; skipped files print `bf: FILE: ERROR` and exit 1 after streaming remaining rows.

### Forget records

```bash
bf forget mail:1a167922 memories/mail/9f2c...json --dry-run
bf forget mail:1a167922
```

Deletes each record (`SOURCE:ID` or `memories/SOURCE/SHA256.json`), records its id's SHA-256 in `memories/SOURCE/.forgotten` so future collections skip it, refreshes the cache and returns `ok`, `dry_run`, `records` (`ref`, `removed`, `listed`) and `index`.

### Shell completion

```bash
eval "$(_BF_COMPLETE=source_bash bf)"   # ~/.bashrc
eval "$(_BF_COMPLETE=source_zsh bf)"    # ~/.zshrc
_BF_COMPLETE=source_fish bf | source    # ~/.config/fish/config.fish
```

## Exit codes and errors

- **Stdout**: compact UTF-8 JSON (plain text for `--help` and `--version`, interactive UI for `watch`, JSONL for `watch --json` and `export`), escaping DEL/C1 controls, U+2028/U+2029 and Unicode format (`Cf`) characters as `\uXXXX`.
- **Stderr**: one `bf:` line per diagnostic naming file/argument and reason without quoting content.

| Code  | Meaning                                                                                                                                                                                       |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`   | Success.                                                                                                                                                                                      |
| `1`   | Operation or check failed (`validate` problems, `eval` case failures, `status --check` unhealthy, program/delivery failure, skipped cache/export files, non-current `skill install --check`). |
| `2`   | Invalid CLI arguments, flags, scopes or malformed refs (`bf: invalid input: --limit: … (see bf search -h)`).                                                                                  |
| `130` | Interrupted by Ctrl-C, SIGTERM or terminal hangup.                                                                                                                                            |

Repeated single-value options (such as two `--scope` flags) and empty selector arguments (`--brain ""`, `bf collect ""`) exit 2; only `--job` on `update` and `watch` repeats.

## Using replies

Replies format timestamps with your local offset (`retrieval`, `status` and `bf schema --kind search-reply|read-reply|status-reply|doctor-reply`).

- **`update` replies**: `ok`, `dry_run`, `brain`, `jobs` (`status` in `collected|ran|due|skipped|failed|gated`), optional `manual` / `deliveries`, and refreshed `index`.
- **`run` replies**: `job`, `run`, `status` (`ok|gated`), optional `action`, `delivery`, `quiet`, `text` (`run --hook`: `ok`, `dry_run`, `hook`, `jobs`).

### Job and source status {#status-sources-and-routines}

`bf status` reports the selected brain's health (`healthy` is `false` whenever the brain has `problems`, non-empty `attention`, or a `stale`/`missing` cache):

| Field                          | Meaning                                                                                                                                    |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `cache`, `pending_transaction` | `ready`, `busy`, `stale` or `missing`; `pending_transaction: true` when `memories/.pending` awaits `bf build`.                             |
| `runtime`                      | `installed` BF version and optional `pinned` version parsed from `uv.lock`.                                                                |
| `attention`                    | Uniform health items needing action ([attention entries](#attention-entries)).                                                             |
| `jobs`                         | Per-job `enabled`, `emit`, `freshness`, `every`, `last_success`, `next_due`, `runs`, `action`, `last_run` and failure details.             |
| `sources`                      | Per-source `state` (`active`/`disabled`/`historical`), `records`, `bytes`, `latest`, `changed`, `silent`, `degraded`, `mode` and `window`. |
| `queue`                        | Durable runtime queue summary (`pending`, `failed`, `uncertain`, `wakes`, `active_runs`, `oldest_pending`).                                |
| `usage`, `warnings`            | `7d` and `30d` `search`/`empty`/`read` counts by kind plus directory scan warnings (>80,000 entries).                                      |

#### Attention entries

Every entry in `attention` (across `bf status`, Home `bf read`, and `bf read review`) uses one uniform shape:

```json
{ "kind": "job", "name": "mail", "freshness": "never", "failed": true }
```

- `kind` is `"job"`, and `name` is the configured identifier.
- Optional `"silent": true` and `"degraded": true` mark silent or 3x-incomplete scheduled record sources.
- Includes scheduled jobs that `failed`, are overdue, or are due and `never` succeeded, plus unresolved runtime failures or daemon errors. Disabled jobs do not emit `attention` entries.

#### Queue {#messaging}

`bf status` reports `queue` counters without revealing message text; run `bf queue` to list unresolved deliveries, pending wakes, or uncertain runs (`bf queue ID` inspects one item).
