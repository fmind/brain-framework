# Reference — commands, schemas, formats, and exit codes

Complete lookup for Brain Framework v25 (`bf.yaml` and `evals/*.yaml` `version: 11`) commands, directory layout, schemas, link syntax, and exit codes.

## CLI commands

`bf --help` and `bf COMMAND -h` print options offline without opening a brain. Boolean flags are positive-only (`--check`, `--force`, `--dry-run`, `--retry`, `--cancel`, `--watch`, `--link`, `--snapshot`, `--allow-removal`, `--stdin`, `--json`, `--full`).

| Command                                                                                                               | Role                                                                                    |
| --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `bf init PATH [--name NAME] [--full]`                                                                                 | Create a brain in a new, empty, or freshly cloned folder (`--full` adds optional dirs)  |
| `bf register [PATH] [--name NAME] [--default]`                                                                        | Register a brain path in `~/.config/bf/config.yaml`                                     |
| `bf unregister TARGET`                                                                                                | Remove a brain registration without modifying brain files                               |
| `bf mcp [--brain BRAIN]`                                                                                              | Run local stdio MCP server (`search` and `read` only; needs `[mcp]` extra)              |
| `bf guide [TOPIC]`                                                                                                    | List topics or print one packaged Markdown guide                                        |
| `bf skill install [--brain BRAIN] [--global] [--check] [--force]`                                                     | Install, check, or overwrite `/bf` skill files                                          |
| `bf schema [--kind brain\|registry\|eval\|search-reply\|read-reply\|status-reply\|doctor-reply]`                      | Print draft 2020-12 JSON Schema (`brain` default)                                       |
| `bf search QUERY [--scope SCOPE] [--limit N] [--offset N]`                                                            | Offline BM25 word and explicit-identity search                                          |
| `bf read [REF] [--rel RELATION] [--offset N]`                                                                         | Offline read of Home, `review`, `tasks`, periods, notes/`#section`, records, `--rel`    |
| `bf export [--kind edges\|identities\|items] [--scope SCOPE] [--observed-since TIME]`                                 | Stream JSON Lines claims, identities, or items from `.bf/cache.sqlite3`                 |
| `bf action new SLUG [--title T] [--summary S] [--unique] [--run ID]`                                                  | Create validated `draft` `actions/YYYY-MM-DD_SLUG/ACTION.md`                            |
| `bf action status SLUG draft\|stable\|deprecated`                                                                     | Update action status and refresh cache atomically                                       |
| `bf collect JOB [--since T] [--until T] [--dry-run] [--allow-removal] [--reconcile] [--input FILE]`                   | Run one `emit: records` job (or check `--input` fixture offline)                        |
| `bf ingest SOURCE [--input FILE] [--snapshot] [--allow-removal] [--dry-run]`                                          | Validate & ingest JSON/JSONL records directly (`-` stdin) without runtime DB access     |
| `bf run [JOB] [ARGS...] [--hook EVENT] [--stdin] [--dry-run] [--allow-removal] [--reconcile] [--since T] [--until T]` | Run one job or all enabled `--hook EVENT` jobs                                          |
| `bf wake [--job JOB] [--in DUR\|--at ISO] [--reason TEXT] [--run ID] [--key K]`                                       | Queue a bounded follow-up wake                                                          |
| `bf post [TEXT] [--to JOB] [--thread ID] [--target ID] [--reply-to ID] [--run ID] [--key K] [--stdin]`                | Enqueue and deliver one outbound message through a `send:` job                          |
| `bf queue [ID] [--retry\|--cancel] [--limit N]`                                                                       | Inspect or explicitly retry/cancel queued runs, wakes, and deliveries                   |
| `bf update [--job NAME] [--dry-run]`                                                                                  | Run due record/scheduled/event jobs, wakes, and deliveries, then refresh cache          |
| `bf watch [--job NAME] [--interval S] [--poll-interval S] [--notify off\|failure\|success\|all] [--json]`             | Run daemons and due jobs continuously (or observe second session read-only)             |
| `bf forget REF... [--dry-run]`                                                                                        | Delete records and store SHA-256 tombstones in `.forgotten`                             |
| `bf status [--check] [--watch]`                                                                                       | Inspect cache, notes, jobs, queue summary (`incidents`), logs, and usage offline        |
| `bf doctor`                                                                                                           | Check executables, `requires.bins`/`env` presence, scripts, and `prompt:` files offline |
| `bf validate`                                                                                                         | Validate `bf.yaml`, OKF frontmatter, links, identities, actions, records, and jobs      |
| `bf eval [--path evals] [--baseline FILE]`                                                                            | Run offline retrieval suites (`evals/*.yaml`)                                           |
| `bf build [--reproject JOB [--dry-run]]`                                                                              | Recover `.pending/`, rebuild `.bf/cache.sqlite3`, or re-validate stored records         |

Most brain commands also accept `--brain NAME|PATH` (`--brain` -> `BF_BRAIN` -> enclosing `bf.yaml` -> registry `default` in `~/.config/bf/config.yaml`). `--dry-run` on `bf update` executes nothing; `--dry-run` on `bf collect` and `bf run` executes the configured job without committing records, actions, or messages.

### Time and duration inputs

- Instants (`--since`, `--until`, `--observed-since`): `now`, `today`, `yesterday`, relative spans (`12h`, `7d`, `2w`), `YYYY-MM-DD`, or timezone-aware ISO 8601.
- Durations in `bf.yaml` / `bf wake --in`: integer seconds or `<n>s|m|h|d|w` (`30s`, `15m`, `6h`, `7d`, `1w`).

## Brain directory and `.bf/` layout

```text
<brain>/
  bf.yaml                 # version: 11, name:, defaults:, watch:, validate:, fields:, jobs:
  AGENTS.md               # Brain agent instructions
  .gitignore              # Ignores /.bf/, /memories/, /actions/, /logs/, /originals/, /inputs/
  projects/               # Active project notes (OKF Markdown)
  concepts/               # Concepts, entities, and concepts/index.md (Home)
  actions/                # actions/YYYY-MM-DD_slug/ACTION.md sessions
  memories/<source>/      # SHA256.json collected records + optional .forgotten tombstones
  sensors/ routines/ prompts/ channels/ evals/ logs/
  .bf/                    # Mode 0700 private state (gitignored)
    cache.sqlite3         # Disposable offline search & graph index (index.SCHEMA = 43)
    runtime.sqlite3       # Durable queue, runs, events, wakes, deliveries, incidents (VERSION = 4)
    usage.sqlite3         # Daily aggregate operation counts (30d retention; no queries/refs)
    wakeup                # Atomic timestamp file touched by collect, ingest, and Queue.admit
    state/<job>/          # {{state}} directory per job
    locks/                # Per-brain exclusive writer locks
```

## `bf.yaml` (`version: 11`) schema {#bf-yaml-schema}

YAML parsing enforces YAML 1.2 core schema via libyaml (rejecting duplicate keys, anchors/aliases, and custom tags):

- **Top level**: `version: 11` (required), `name` (required slug), `defaults` (`harness`, `timezone`, `post`), `watch` (`interval`, `poll_interval`, `notifications`, `notification_cooldown`), `validate` (`max_stale_days`, `warn_stale_days`, `index_note`, `required_status`, `forbid_patterns`), `fields` (`{ description, broader, targets }` relation constraints), `jobs` (up to 128 named jobs).
- **`on:` triggers**: interval `{ every }`, wall-clock `{ at: "HH:MM", days?, timezone? }`, CLI hook `{ hook }`, daemon stream `{ daemon: true }`, or runtime event `{ event: { kind, source?, match?, cooldown?, max?, expires? } }`.
- **Canonical `event.kind`**: `"record.added"`, `"record.changed"`, `"record.removed"`, `"note.changed"`, `"message.received"`, `"job.failed"` (plus `"record"` shorthand). `match` is an offline FTS5 query string (`<= 1,024` chars).
- **`evals/*.yaml`**: requires `version: 11` and `cases:` (`name`, `query`/`read`, `scope`, `limit`, `expect`, `forbid`, `text`, `empty`).

## OKF notes, records, identities, and `?rel=` syntax

- **OKF notes**: require `type` and `status` (`draft|stable|deprecated`); optional `title`, `updated` (`YYYY-MM-DD`), `summary`, `stale_after` (tz-aware ISO 8601), `entity`, `aliases`, `resource`, `tags`, `sources`, `verified`, `relations`, `attributes`.
- **Records (`memories/SOURCE/<sha256>.json`)**: require `id` and single-line `title`; optional `text`, `time` (tz-aware ISO), `url`, `@type` (`[A-Za-z][A-Za-z0-9_-]*`), `links`, `aliases`, `relations`, `attributes` (`updated`, `observed`, `partial` are reserved).
- **Links & identities**: `bf://BRAIN/PATH?rel=REL#SECTION` (`?rel=` is the only supported query param; custom heading IDs use `## Heading {#id}`). Canonical identities use `scheme:value` (recommended schemes: `person:email/`, `person:HOST/`, `repo:HOST/OWNER/NAME`, `label:...`). Reserved built-in relations: `tagged-with`, `links`, `cites`.

## Shell completion, diagnostics, and exit codes

- **Completion**: `eval "$(_BF_COMPLETE=source_bash bf)"` (`source_zsh` / `source_fish` also supported).
- **Stdout / stderr**: commands print compact single-line JSON on stdout (plain text for `--help`, `--version`, `bf guide`); diagnostics write `bf:` lines on stderr quoting file/field only and escaping control/Unicode format characters.
- **Exit codes**: `0` success (and no `--hook` listeners), `1` failure or unhealthy `--check`, `2` invalid input or malformed arguments, `130` interrupted (`SIGINT` / `SIGTERM`).
