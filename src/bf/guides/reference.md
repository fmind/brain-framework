# Reference

Complete contract lookup for Brain Framework (`bf`), flat brain layout, `bf.yaml` (`version: 12`), pure JSON `memories/**/*.json` and `logs/*.json` schemas, and exit codes.

## CLI commands

Every command writes one compact UTF-8 JSON object to `stdout` (except `bf --version`, `--help`, and live `bf watch`) and single-line `bf: ...` diagnostics to `stderr`.

- `bf --version`: print the installed package version.
- `bf init PATH [--name NAME]`: scaffold a flat `version: 12` brain with `bf.yaml`, `.gitignore`, `AGENTS.md`, `concepts/welcome.md`, `docs/index.md`, `evals/smoke.yaml`, `schemas/{record,concepts,logs,evals}.json`, `skills/bf/SKILL.md` (`.agents/skills/bf`), and `memories/concepts/welcome.json`.
- `bf build [--brain PATH]`: compile `projects/**/*.md`, `concepts/**/*.md`, and `actions/*/ACTION.md` into `memories/{projects,concepts,actions}/<slug>.json`, pruning deleted notes.
- `bf ingest SOURCE [--input FILE] [--mode upsert|snapshot] [--allow-removal] [--dry-run] [--brain PATH]`: validate records against `schemas/record.json` + optional `schemas/<source>.json` and write `memories/<source>/<sha256(id)>.json`.
- `bf validate [--brain PATH]`: read-only check of `bf.yaml`, `schemas/*.json`, OKF notes, `memories/**/*.json`, `logs/*.json`, `evals/*.yaml`, and local/`bf://` links.
- `bf review [--brain PATH]`: read-only report of `stale` notes (`newer_evidence`, `due`, `future_updated`), open `draft` `actions`, and `tasks_open`.
- `bf read [TARGET] [--brain PATH]`: inspect the brain overview (`counts` and failed-unit `attention`) or resolve a note path, `#anchor`, `source:id` record, cross-brain `bf://<host>/<path>?...#<anchor|jq>`, or `#<jq_filter>` fragment.
- `bf query [PATTERN] [--jq EXPR | --sql SQL] [--scope PATH] [--limit N] [--brain PATH]`: run `rg`, `jq`, or sandboxed `duckdb` over `memories/**/*.json`.
- `bf action TOPIC [--status draft|stable|deprecated] [--summary TEXT] [--brain PATH]`: create or reuse `actions/YYYY-MM-DD_<slug>/ACTION.md` (with `inputs/` and `outputs/`).
- `bf eval [SUITE] [--brain PATH]`: run deterministic query checks in `evals/*.yaml`.
- `bf run [JOB ...] [--due] [--hook pre-commit|pre-push] [--dry-run] [--brain PATH] [-- EXTRA_ARGS...]`: execute configured jobs, due schedules/events, or Git hooks.
- `bf status [--check] [--brain PATH]`: inspect `"inside_brain"`, `events_seq`, `jobs`, `channels`, and `servers` (`--check` exits `1` on failed units or missing `requires` binaries).
- `bf post [TEXT] [--stdin] [--to CHANNEL] [--thread ID] [--reply-to ID] [--run ID] [--dry-run] [--brain PATH]`: send an outbound message via `channels.<name>.send` (`--to` is optional when a single enabled channel defines `send`).
- `bf watch [--once] [--json] [--brain PATH]`: run the `watchfiles` supervisor and Rich TUI dashboard (`--once` runs one tick and prints JSON; `--json` streams tick lines).
- `bf mcp [--transport stdio|http] [--port PORT] [--brain PATH]`: run the read-only MCP server (`bf_review`, `bf_read`, `bf_query`, `bf_validate`).

Brain selection resolves `--brain PATH` -> `BF_BRAIN` -> nearest enclosing `bf.yaml` -> `DEFAULT_BRAIN_PATH` (zero global config).

## Exit codes

- `0`: success (`valid: true` on `bf validate`, `ok: true` on `bf eval` / `bf run` / `bf status --check`).
- `1`: validation failure, failed job/eval, missing/corrupt files, schema violation, or lock contention.
- `2`: invalid CLI flags or malformed argument syntax (`bf: invalid input: ...`).
- `130`: interrupted (`SIGINT`).

## Flat brain layout

`projects/`, `concepts/`, `actions/`, `memories/`, `logs/`, `evals/`, `docs/`, `skills/`, `schemas/`, optional operational folders created as needed (`sensors/`, `routines/`, `channels/`, `prompts/`, `settings/`), optional gitignored `.env`, plus private `.bf/` (`0700`: `locks/`, `state/`).

## `bf.yaml` schema {#bf-yaml-schema}

Published at [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json):

```yaml
version: 12
name: my-brain
brains:
  team-brain: ../team-brain
defaults:
  timezone: UTC
  env: {}
jobs: {}
channels: {}
servers: {}
watch:
  interval: 5s
  debounce: 300ms
```

## Published JSON Schemas

- [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json): `bf.yaml` (`Config`).
- [`record.schema.json`](https://fmind.github.io/brain-framework/record.schema.json): `schemas/record.json` (`Record` — frontmatter + `headings`, keyed `attrs`, `elements`, `links`, `tasks`, `text`, `sha256`).
- [`concepts.schema.json`](https://fmind.github.io/brain-framework/concepts.schema.json): `schemas/concepts.json` (`Concept` — user-defined concept metadata).
- [`logs.schema.json`](https://fmind.github.io/brain-framework/logs.schema.json): `schemas/logs.json` (`Log` — `logs/<name>.json` and `logs/events.json`).
- [`evals.schema.json`](https://fmind.github.io/brain-framework/evals.schema.json): `schemas/evals.json` (`Eval` — `evals/*.yaml` deterministic checks).
