# Reference — commands, `bf.yaml`, OKF, record JSON, state, and exit codes

Compact lookup for Brain Framework v24 (`bf.yaml` `version: 10`) contracts, file layouts, schemas, and exit codes.

## CLI commands by task

| Group         | Command                                                                                                                      | Purpose                                                                                   |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Read & search | `bf search QUERY [--scope SCOPE] [--limit N] [--offset N]`                                                                   | Full-text and explicit identity search                                                    |
| Read & search | `bf read [REF] [--rel REL] [--offset N]`                                                                                     | Read `home`, `review`, `tasks`, periods (`7d`), note/`#section`, record, or `--rel` edges |
| Read & search | `bf export [--kind edges\|identities\|items] [--scope SCOPE] [--observed-since T]`                                           | Stream JSON Lines graph or records offline                                                |
| Authoring     | `bf action new SLUG [--title T] [--summary S] [--unique] [--run RUN_ID]`                                                     | Create validated `draft` note at `actions/YYYY-MM-DD_SLUG/ACTION.md`                      |
| Authoring     | `bf action status SLUG draft\|stable\|deprecated`                                                                            | Update action status atomically and refresh index                                         |
| Execution     | `bf collect JOB [--since T] [--until T] [--dry-run] [--reconcile] [--allow-removal] [--input FILE]`                          | Run one record job or validate fixture JSON (`map:` applied if configured)                |
| Execution     | `bf run [JOB] [-- ARGS] [--hook EVENT] [--since T] [--until T] [--dry-run] [--stdin] [--reconcile]`                          | Run one job or subscribed hook jobs                                                       |
| Execution     | `bf post TEXT [--to JOB] [--thread T] [--target TARGET] [--run RUN_ID] [--key K] [--stdin]`                                  | Send one message durably through a `send:` job                                            |
| Execution     | `bf wake [AT] [--in DURATION] [--job JOB] [--reason TEXT] [--run RUN_ID] [--key K]`                                          | Queue a follow-up wake for an enabled job                                                 |
| Execution     | `bf queue [ID] [--retry\|--cancel] [--limit N]`                                                                              | Inspect or resolve durable `.bf/runtime.sqlite3` runs, wakes, and deliveries              |
| Execution     | `bf update [--job JOB] [--dry-run]`                                                                                          | Run due record/scheduled/event jobs, wakes, and deliveries, then refresh cache            |
| Execution     | `bf watch [--job JOB] [--interval S] [--poll-interval S] [--notify MODE] [--json]`                                           | Run due jobs, daemons, and events continuously on one brain                               |
| Operations    | `bf status [--check] [--watch]`, `bf doctor`                                                                                 | Check brain health, registration, or offline binary/env/script prerequisites              |
| Operations    | `bf validate`                                                                                                                | Validate `bf.yaml`, OKF frontmatter, links, and records                                   |
| Operations    | `bf eval`                                                                                                                    | Run offline retrieval benchmarks in `evals/*.yaml`                                        |
| Operations    | `bf build [--reproject SOURCE]`                                                                                              | Rebuild `.bf/cache.sqlite3` or reproject cached records                                   |
| Operations    | `bf forget REF... [--dry-run]`                                                                                               | Remove collected records and keep tombstones                                              |
| Setup & docs  | `bf init PATH`, `bf register [PATH] [--name N] [--default]`, `bf unregister TARGET`, `bf skill install [--global]`, `bf mcp` | Create, register/unregister, install `/bf`, and run stdio MCP server                      |
| Setup & docs  | `bf guide [TOPIC]`, `bf schema [--kind KIND]`                                                                                | Read packaged guides or inspect JSON Schemas                                              |

## Brain directory & `.bf/` state layout

```text
<brain>/
  bf.yaml                   # version: 10, defaults:, watch:, validate:, fields:, jobs:
  AGENTS.md                 # Agent instructions for this brain
  .gitignore                # Ignores /.bf/, /memories/, /actions/, /logs/, /originals/, /inputs/
  projects/                 # Active project notes (OKF Markdown)
  concepts/                 # Durable concepts and concepts/index.md (home page)
  actions/                  # actions/YYYY-MM-DD_slug/ACTION.md tracked sessions
  memories/<source>/        # Atomic SHA256.json records written by record jobs (never hand-edited)
  sensors/ routines/ prompts/ channels/ evals/ logs/
  .bf/                      # Internal local state (mode 0700; never committed to Git)
    cache.sqlite3           # Disposable offline SQLite retrieval/graph cache
    runtime.sqlite3         # Durable job runs, messages, deliveries, and wakes (never rebuilt on error)
    usage.sqlite3           # Offline query/read telemetry (stores no matched text)
    state/<job>/            # {{state}} directory per job
    locks/                  # Per-brain exclusive writer locks
```

## Schema shapes (`bf schema --kind KIND`)

Run `bf schema --kind brain` (or `registry`, `record`, `eval`, `search-reply`, `read-reply`, `status-reply`, `doctor-reply`) for exact machine-checked JSON Schemas:

- **`bf.yaml` (`brain`)**: requires `version: 10`; `defaults:` sets shared `harness`, `timezone`, `post`; `jobs:` defines unified jobs (`run`/`prompt`/`send`, `emit`, `on`, `gate`, `map`, `extract`, `post`, `requires`, `timeout`, `max_bytes`, `retries`, `idempotent`); `fields:` is purely optional relation metadata (`description`, `broader`, `targets`). Any valid relation name (`^[a-z][a-zA-Z0-9]*(?:-[a-z0-9]+)*$`, 1–64 chars, except reserved `tagged-with`, `links`, `cites`) works everywhere with zero declaration.
- **Registry (`~/.config/bf/config.yaml`)**: holds optional `default:` brain name and `brains:` map (`bf register`).
- **OKF notes**: YAML frontmatter requires `type` and `status` (`draft`, `stable`, `deprecated`); optional `relations` (explicit `scheme:value` or `bf://` targets) and `attributes` (arbitrary JSON metadata, never graph edges).
- **Collected records (`memories/<source>/<sha256>.json`)**: provenance (`updated`, `observed`, optional `partial`, `generated`, `url`, `created`, `title`, `text`), optional `@type` (`[A-Za-z][A-Za-z0-9_-]*`), `relations` (explicit `scheme:value` or `bf://` targets), and `attributes` (arbitrary JSON metadata).

## Addresses, refs, and `?rel=` queries

- Note refs: relative file paths (`projects/api.md`) with optional section fragment (`projects/api.md#decision`).
- Record refs: `SOURCE:ID` (for example `gmail:msg-123` or `memories/SOURCE/SHA256.json`).
- Canonical identities: `scheme:value` or `bf://...`.
- Typed Markdown links: `[label](../concepts/auth.md?rel=depends-on)`; `?rel=` is the only supported query parameter.

## Exit codes and output format

- Standard output: compact single-line JSON for commands (plain text for `--help`, `--version`, and `bf guide`).
- Standard error: `bf:` diagnostics naming file/position without quoting untrusted record or provider text (control and Unicode format characters escaped).
- Exit codes: `0` success, `1` failure or unhealthy `--check`, `2` invalid user input or malformed arguments, `130` interrupted (`SIGINT` / `SIGTERM`).
