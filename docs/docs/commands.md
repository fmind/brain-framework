---
icon: lucide/square-terminal
description: Look up Brain Framework commands, previews, graph exports, replies, errors and exit codes.
---

# Command reference

`bf --help` lists every command; `bf COMMAND -h` lists arguments, defaults and choices without reading a brain or running programs. Boolean flags are positive-only (`--check`, `--force`, `--dry-run`, `--retry`, `--cancel`, `--watch`, `--link`, `--allow-removal`, `--stdin`, `--json`, `--full`).

## Commands

| Command                                                                                     | Purpose                                                                                                                                 |
| ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `init PATH [--name NAME] [--full]`                                                          | Create a brain in a new, empty or freshly cloned folder; `--full` adds optional folders.                                                |
| `register [PATH]`                                                                           | Add or update a brain's entry in the optional [machine registry](configuration.md#optional-machine-registration).                       |
| `mcp`                                                                                       | Serve read-only `search` and `read` over MCP stdio; needs the [`mcp` extra](mcp.md#before-connecting).                                  |
| `skills DIR [--check] [--force] [--link]`                                                   | Install or update packaged agent skills; `--link` inside a brain also manages `.agents/skills/bf-*` and `.claude/skills/bf-*` symlinks. |
| `action new SLUG [--title TITLE] [--summary SUMMARY] [--unique]`                            | Create a validated `status: draft` `actions/YYYY-MM-DD_SLUG/ACTION.md` note (`--unique` adds a 12-hex suffix) and refresh the cache.    |
| `action status SLUG draft\|stable\|deprecated`                                              | Update an action's OKF `status` atomically (`draft` = open on Home, `stable` = settled, `deprecated` = cancelled).                      |
| `schema [--kind KIND]`                                                                      | Print a configuration or [reply schema](retrieval.md#reply-schemas) (`bf.yaml` by default).                                             |
| `search QUERY [--scope SCOPE] [--limit N] [--offset N]`                                     | Search words or an identity, optionally within one [scope](retrieval.md#scopes).                                                        |
| `read [REF] [--rel RELATION] [--offset N]`                                                  | Open Home, `review`, another page, note, `#section`, record or identity; `--rel` lists incoming links of one relation.                  |
| `export [--kind edges\|identities\|items] [--scope SCOPE] [--observed-since TIME]`          | Stream graph edges, multi-name identities or full items as JSON Lines from the caches.                                                  |
| `collect SENSOR [--since TIME] [--until TIME] [--dry-run] [--allow-removal] [--input FILE]` | Run one sensor now, or [validate saved JSON/JSONL output](sensors.md#check-saved-output) without running it.                            |
| `forget REF... [--dry-run]`                                                                 | Remove collected records and block future collection via `.forgotten`.                                                                  |
| `run [PROGRAM] [ARGS]... [--hook EVENT] [--stdin] [--dry-run]`                              | Run one routine or [assistant](prompts.md), or every routine listing a Git hook.                                                        |
| `channel read CHANNEL --since START [--until END]`                                          | Read one complete channel batch without advancing checkpoints or invoking assistants.                                                   |
| `channel send CHANNEL --stdin`                                                              | Persist and send one outgoing JSON message through its configured endpoint.                                                             |
| `queue [ID] [--retry\|--cancel] [--limit N]`                                                | List jobs, deliveries and wakes, open one item in detail, or retry/cancel it.                                                           |
| `update [--sensor NAME] [--routine NAME] [--assistant NAME] [--kind KIND] [--dry-run]`      | Run due sensors, due routines and due assistant work, then refresh the cache.                                                           |
| `watch [--interval N] [--poll-interval N] [--notify MODE] [--json]`                         | Continuously run due programs and channel streams with a live dashboard or JSONL stream ([watch](schedule.md)).                         |
| `status [--check] [--watch]`                                                                | Report caches, record counts, program health, logs and local usage.                                                                     |
| `validate`                                                                                  | Check notes, links, identities, actions, records and program files.                                                                     |
| `eval [--path evals] [--baseline FILE]`                                                     | Run retrieval test cases and compare ranks against an optional baseline.                                                                |
| `build [--reproject SENSOR [--dry-run]]`                                                    | Recover interrupted writes and rebuild `.bf/`; `--reproject` [re-applies field mappings](schema.md#reproject-stored-records).           |

Brain selection ([Configuration](configuration.md#select-a-brain)):

- `collect`, `run`, `action`, `channel`, `queue`, `update`, `watch`, `forget`, `build`, `validate`, `eval` and `status --watch` act on exactly one brain.
- `search`, `read`, `export`, `mcp` and `status` cover selected brains (and their direct `brains:` references for retrieval).

### Preview an update or a collection

```bash
bf update --dry-run
bf collect local-documents --dry-run
```

| Preview             | Executes                       | Result                                                                            |
| ------------------- | ------------------------------ | --------------------------------------------------------------------------------- |
| `update --dry-run`  | Nothing.                       | Lists due programs with windows, plus enabled manual programs under `manual`.     |
| `collect --dry-run` | **The sensor.**                | Previews counts and up to three sample records without saving records or history. |
| `run --dry-run`     | **The routines or assistant.** | Writes no action, record or delivery; returns generated Markdown/reply as `text`. |

`--since`, `--until` and `--observed-since` accept `now`, `today`, `yesterday`, `12h`, `7d`, `2w`, `YYYY-MM-DD` and timezone-aware ISO 8601 instants. `collect` accepts a JSON array, JSON Lines (one JSON object per line) or `{records, incomplete}` on sensor stdout and `--input FILE` (`-` for stdin).

### Export the graph

`bf export` reads validated caches offline across selected brains and their direct `brains:` references, emitting one JSON object per line:

- **`--kind edges` (default)**: `brain`, `subject`, `relation` (declared relation, `cites`, `tagged-with` or `links`), `target`, `origin`, plus `date` (notes) or `time`/`observed` (records), sorted by subject, relation, target and origin.
- **`--kind identities`**: every note or record answering to more than its own file/ref address (`brain`, `kind`, `names`, `ref`, `status`, `type`).
- **`--kind items`**: every note and record sorted by `ref`, carrying metadata, single-value `fields`, multi-value `values` (`dict[str, list[str]]`), stored record `attributes` (`dict[str, Any]`) and full `text` (plus `"partial": true` when truncated):

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
  "values": { "participant": ["person:email/alice@example.test"] },
  "attributes": { "observed": "2026-09-29T13:10:00Z", "status": "open" },
  "text": "The launch moves to Monday."
}
```

- **`--scope SCOPE`**: restricts output to items matching [`bf search --scope SCOPE`](retrieval.md#scopes).
- **`--observed-since TIME`** (`--kind items`): keeps collected records (`kind: "record"`) whose `observed` timestamp is at or after the cutoff, so gates and external indexers read incremental batches without scanning older items.

Changed/unreadable files since the last cache refresh omit `text` with `bf: BRAIN: FILE: text omitted: …` on stderr; skipped files print `bf: BRAIN: FILE: ERROR` and exit 1 after streaming remaining rows.

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

| Code  | Meaning                                                                                                                                                                        |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `0`   | Success.                                                                                                                                                                       |
| `1`   | Operation or check failed (`validate` problems, `eval` case failures, `status --check` unhealthy, program/delivery failure, skipped cache/export files, non-current `skills`). |
| `2`   | Invalid CLI arguments, flags, scopes or malformed refs (`bf: invalid input: --limit: … (see bf search -h)`).                                                                   |
| `130` | Interrupted by Ctrl-C, SIGTERM or terminal hangup.                                                                                                                             |

Repeated single-value options (such as two `--scope` flags) and empty selector arguments (`--brain ""`, `bf collect ""`) exit 2; only `--sensor`, `--routine`, `--assistant` and `update --kind` repeat.

## Using replies

Replies format timestamps with your local offset (`retrieval`, `status` and `bf schema --kind search-reply|read-reply|status-reply`).

- **`update` replies**: `ok`, `dry_run`, `brain`, `sensors`, `routines`, optional `assistants` (`status` in `ran|due|skipped|failed|gated`), `manual` and refreshed `index`.
- **`run` replies**: `ok`, `dry_run`, optional `hook`, and `routines` or `assistants` (`status`, optional `record`/`action`, `text` or `error`).

### Program and source status {#status-sources-and-routines}

`bf status` groups per-brain health under `brains` (`healthy` is `false` whenever any brain has `problems`, non-empty `attention`, a `stale`/`missing` cache, or an unreachable brain/reference):

| Field                                | Meaning                                                                                                                                                    |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cache`, `pending_transaction`       | `ready`, `busy`, `stale` or `missing`; `pending_transaction: true` when `memories/.pending` awaits `bf build`.                                             |
| `runtime`                            | `installed` BF version and optional `pinned` version parsed from `uv.lock`.                                                                                |
| `attention`                          | Uniform health items needing action ([attention entries](#attention-entries)).                                                                             |
| `state`, `freshness`                 | `active`/`disabled`/`historical`; `fresh`, `overdue`, `never`, `manual` or `unknown` ([timing](schedule.md#timing-and-health)).                            |
| `records`, `bytes`, `latest`         | Indexed record count, disk bytes and newest event timestamp.                                                                                               |
| `changed`, `silent`, `degraded`      | Last record addition/change time, `silent: true` past `silent_after`, and `{runs, missed, of}` for partial runs.                                           |
| `mode`, `window`, `reconciled`       | `window` or `snapshot`, contiguous `{since, until}` coverage, and last scheduled reconciliation timestamp.                                                 |
| `last_collected` / `last_success`    | Latest current collection (sensors) or successful run (routines/assistants).                                                                               |
| `last_run`                           | Last committed sensor counters (`records`, `added`, `updated`, `unchanged`, `removed`, `forgotten`, `expired`, window, `elapsed_seconds`, `output_bytes`). |
| `action` / `runs`, `record`          | Latest routine action path / assistant run count and latest record ref.                                                                                    |
| `schedules`                          | Assistant schedules: `every` or `at`, `next_due`, `last_success` and `freshness`.                                                                          |
| `failed`, `error`, `failures`, `log` | Failure state, diagnostic, consecutive failure count and `logs/NAME.log`.                                                                                  |
| `messaging`                          | Durable queue summary ([messaging](#messaging)).                                                                                                           |
| `usage`, `warnings`                  | `7d` and `30d` `search`/`empty`/`read` counts by kind plus directory scan warnings (>80,000 entries).                                                      |

#### Attention entries

Every entry in `attention` (across `bf status`, Home `bf read`, and `bf read review`) uses one uniform shape:

```json
{ "kind": "sensor", "name": "mail", "freshness": "never", "failed": true }
```

- `kind` is `"sensor"`, `"routine"`, `"assistant"` or `"channel"`, and `name` is the configured identifier.
- Optional `"silent": true` and `"degraded": true` mark silent or 3x-incomplete scheduled sources.
- Includes scheduled programs that `failed`, are `overdue`, or are due and `never` succeeded; assistants with `failed`/`uncertain` jobs or unstartable schedules; and channels with `failed`/`uncertain` deliveries or active `read`/`stream` errors. Disabled programs do not emit `attention` entries themselves.

#### Messaging

When `queue.sqlite3` exists, `messaging` reports:

- `jobs` (latest 50) and `deliveries` (next 50 unresolved) with states and errors, plus `counts`, `delivery_counts` and `delivery_failures`.
- `transports`: active `health:*` errors with timestamp `at`.
- `wakes`: pending per-slot wakes (`assistant`, `due`, `job` or `null`, and `reason`).
