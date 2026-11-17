# Reference

Complete contract lookup for Brain Framework (`bf`), flat brain layout, `bf.yaml` (`version: 13`), pure JSON `memories/**/*.json` schemas, `.bf/state/`, `structlog` JSON logs, and exit codes.

## CLI commands

Every command writes one compact UTF-8 JSON object to `stdout` (except `bf --version`, `--help`, `bf mcp`, and live `bf watch`) and single-line `bf: ...` diagnostics to `stderr`. `--brain PATH` may appear before or after any subcommand except `bf init`.

- `bf --version`: print the installed package version.
- `bf init PATH [--name NAME]`: scaffold a flat `version: 13` brain with `bf.yaml`, `.gitignore`, `AGENTS.md`, `concepts/welcome.md`, `docs/index.md`, `evals/smoke.yaml`, `schemas/{bf.schema.json,record.json,concepts.json,evals.json}`, `skills/bf/SKILL.md` (`.agents/skills/bf`), and `memories/concepts/welcome.json`.
- `bf build [--brain PATH]`: compile `projects/**/*.md`, `concepts/**/*.md`, and `actions/*/ACTION.md` into `memories/{projects,concepts,actions}/<slug>.json`, pruning deleted notes.
- `bf ingest SOURCE [--input FILE] [--mode upsert|snapshot] [--allow-removal] [--dry-run] [--brain PATH]`: validate records against `schemas/record.json` + optional `schemas/<source>.json` and write `memories/<source>/<sha256(id)>.json` (rejects interactive TTY stdin when `--input -`).
- `bf validate [--brain PATH]`: read-only check of `bf.yaml`, `schemas/*.json`, OKF notes, compiled-note sync, `memories/**/*.json`, `evals/*.yaml`, and local/`bf://` links.
- `bf review [--brain PATH]`: read-only report of `stale` notes (`newer_evidence`, `due`, `future_updated`), open `draft` `actions`, and `tasks_open` (exits `1` if `problems` is non-empty).
- `bf read TARGET [--brain PATH]`: resolve an OKF note path (with optional `#anchor`), `source:id` record, JSON memory path, or cross-brain `bf://<host>/<path>?...#<anchor>`.
- `bf query [PATTERN] [--jq EXPR] [-s/--scope PATH] [-n/--limit N] [--brain PATH]`: run `rg` or `jq` over `memories/**/*.json` (`truncated: true` when matches exceed `--limit`).
- `bf eval [SUITE] [--brain PATH]`: run deterministic query checks in `evals/*.yaml`.
- `bf run [JOB ...] [--due | --hook pre-commit|pre-push] [--dry-run] [--brain PATH] [-- EXTRA_ARGS...]`: execute named jobs, rebuild dirty notes and run due/event jobs (`--due`), or run Git hooks (`--hook`); requires one selector.
- `bf status [--check] [--brain PATH]`: inspect `"inside_brain"`, `counts`, `attention`, `events_seq`, `jobs`, `channels`, and `services` (`--check` exits `1` on failed units or missing `requires` binaries).
- `bf watch [--json] [--brain PATH]`: run the operational `watchfiles` supervisor and instant Rich TUI dashboard (auto-emits `structlog` JSON tick lines when non-TTY or `--json`).
- `bf mcp [--transport stdio|http] [--port PORT] [--brain PATH]`: run the read-only MCP server (`bf_status`, `bf_review`, `bf_read`, `bf_query`, `bf_validate`; `--port` requires `--transport http`).

Brain selection resolves `--brain PATH` -> `BF_BRAIN` -> nearest enclosing `bf.yaml` -> `DEFAULT_BRAIN_PATH` (zero global config).

## Exit codes

- `0`: success (`ok: true` on `bf build`, `bf validate`, `bf review`, `bf eval`, `bf run`, `bf status --check`).
- `1`: validation failure, failed job/eval, missing/corrupt files, schema violation, or lock contention.
- `2`: invalid CLI flags, conflicting selectors, TTY stdin on `bf ingest`, or malformed argument syntax (`bf: invalid input: ...`).
- `130`: interrupted (`SIGINT`).

## Flat brain layout

`projects/`, `concepts/`, `actions/`, `memories/`, `logs/` (`*.log` JSON lines via `structlog`), `evals/`, `docs/`, `skills/`, `schemas/`, optional operational folders created as needed (`sensors/`, `routines/`, `channels/`, `prompts/`, `settings/`), optional gitignored `.env`, plus private `.bf/` (`0700`: `locks/`, `state/` for `<unit>.json` and `events.json`).

## `bf.yaml` schema {#bf-yaml-schema}

Published at [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json):

```yaml
version: 13
name: my-brain
brains:
  team-brain: ../team-brain
timezone: UTC
env: {}
jobs: {}
channels: {}
services: {}
watch:
  interval: 5s
  debounce: 300ms
```

## Published JSON Schemas

- [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json): `schemas/bf.schema.json` / `bf.yaml` (`Config`).
- [`record.schema.json`](https://fmind.github.io/brain-framework/record.schema.json): `schemas/record.json` (`Record` — frontmatter + `headings`, keyed `attrs`, `elements`, `links`, `tasks`, `text`, `sha256`).
- [`concepts.schema.json`](https://fmind.github.io/brain-framework/concepts.schema.json): `schemas/concepts.json` (`Concept` — user-defined concept metadata).
- [`evals.schema.json`](https://fmind.github.io/brain-framework/evals.schema.json): `schemas/evals.json` (`Eval` — `evals/*.yaml` deterministic checks).
