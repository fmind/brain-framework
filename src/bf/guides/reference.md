# Reference

Complete contract lookup for Brain Framework (`bf`), flat brain layout, `bf.yaml` (`version: 15`), pure JSON `memories/**/*.json` schemas, `.bf/`, plain JSON line logs, and exit codes.

## CLI commands

Every command writes one compact UTF-8 JSON object to `stdout` (except `bf --version`, `--help`, `bf mcp`, `--format jsonl|tsv`, and live `bf watch`) and single-line `bf: ...` diagnostics to `stderr`. `--brain PATH` may appear before or after any subcommand except `bf init` and `bf schema`.

- `bf --version`: print the installed package version.
- `bf init PATH [--name NAME]`: scaffold a minimal `version: 15` brain (`bf.yaml`, `.gitignore`, `AGENTS.md`, `concepts/welcome.md`, `evals/smoke.yaml`, `schemas/concepts.json`, `skills/bf/SKILL.md` linked at `.agents/skills/bf`, and compiled `memories/concepts/welcome.json`).
- `bf build [--force] [--brain PATH]`: compile `projects/**/*.md`, `concepts/**/*.md`, and `actions/*/ACTION.md` into `memories/{projects,concepts,actions}/<slug>.json` and sync `.bf/index.jsonl`.
- `bf ingest SOURCE [--input FILE] [--mode upsert|snapshot] [--allow-removal] [--dry-run] [--brain PATH]`: validate records against the bundled `record` schema + optional `schemas/<source>.json` and write `memories/<source>/<sha256(id)>.json` (rejects interactive TTY stdin when `--input -`).
- `bf read TARGET [--outline | --lines START[:COUNT] | --raw | --full] [--fields F,...] [--max-bytes N] [--brain PATH]`: resolve an OKF note (or `#anchor`, `START:` reads to EOF), `source:id`, JSON memory path, alias, or cross-brain `bf://<host>/<path>`.
- `bf query [PATTERN] [--jq EXPR] [--kind note|memory] [-s/--scope PATH] [--source NAME] [--fields F,...] [-n/--limit N] [--format json|jsonl|tsv] [--brain PATH]`: run `rg`, `jq`, or combined `rg` + `jq` over `memories/**/*.json` (`--limit` `1..5000`, default `20`).
- `bf validate [--limit N] [--all] [--strict] [--force] [--brain PATH]`: check `bf.yaml`, `.env`, `schemas/*.json`, OKF notes, compiled-note sync, `memories/**/*.json`, `evals/`, and links without compiling notes or mutating `memories/` (updates `.bf/cache/state.json` when writable; `--limit` default `50`; snapshot/forgotten missing targets warn unless `--strict`).
- `bf review [--limit N] [--all] [--fields F,...] [--format json|jsonl|tsv] [--brain PATH]`: report `stale` notes (`newer_evidence`, `due`, `future_updated`), open `draft` `actions_open`, and `total_tasks_open` via `.bf/index.jsonl` (`--limit` default `20`; `jsonl`/`tsv` rows include `"section": "stale"|"action_open"` unless omitted by `--fields`).
- `bf eval [TARGET] [--brain PATH]`: run deterministic query checks in `evals/*.{yaml,yml,json}`.
- `bf check [--strict] [--force] [--brain PATH]`: run incremental `build`, `validate`, and `eval` in one pass.
- `bf new concept|project|action SLUG [--title TITLE] [--type TYPE] [--status STATUS] [--brain PATH]`: scaffold a valid note via `Store` and rebuild compiled memories.
- `bf schema [bf|record|concepts|evals]`: print one or all bundled Draft 2020-12 JSON Schemas (no brain required).
- `bf run [LOOP ...] [--due | --hook pre-commit|pre-push] [--dry-run] [--brain PATH] [-- EXTRA_ARGS...]`: execute named cycle loops, rebuild dirty notes and run due/event loops concurrently up to `watch.concurrency` (`--due`), or run sequential Git hooks (`--hook`).
- `bf emit KIND [--source NAME] [--data JSON] [--dedupe] [--brain PATH]`: append a custom event directly to `.bf/events.jsonl` and `logs/events.log`.
- `bf status [--all] [--fields F,...] [--check] [--brain PATH]`: inspect `"inside_brain"`, compact `counts` (`notes`, `records`, `sources`, `loops`), failing `attention` loops, `orphaned_states`, and optional `loops` (`--all`).
- `bf watch [--json] [--brain PATH]`: run the `watchfiles` supervisor, daemon loops, non-blocking bounded worker pool, and Rich TUI dashboard (auto-emits JSON tick lines when non-TTY or `--json`).
- `bf mcp [--brain PATH]`: run the strictly read-only `stdio` MCP server (`bf_status`, `bf_review`, `bf_read`, `bf_query` `1..200`, `bf_validate`; never mutates brain files; cross-brain links require `bf.yaml` `brains:`).

Brain selection resolves `--brain PATH` -> `BF_BRAIN` -> nearest enclosing `bf.yaml` -> `DEFAULT_BRAIN_PATH` (zero global config).

## Exit codes

- `0`: success (`ok: true` on `build`, `query`, `validate`, `review`, `eval`, `check`, `new`, `run`, `status --check`, `emit`).
- `1`: validation failure, failed loop/eval, missing/corrupt files, schema violation, or lock contention.
- `2`: invalid CLI flags, conflicting selectors, running a `daemon: true` loop via `bf run`, TTY stdin on `bf ingest`, or malformed argument syntax (`bf: invalid input: ...`).
- `130`: interrupted (`SIGINT`).

## Flat brain layout

`projects/`, `concepts/`, `actions/`, `memories/`, `evals/`, `skills/`, `schemas/` (`concepts.json` + optional `<source>.json`), optional operational folders created as needed (`sensors/`, `routines/`, `loops/`, `prompts/`, `settings/`, `docs/`), optional gitignored `.env`, plus private `logs/` (`0700`: `0600` plain JSON line `*.log`) and `.bf/` (`0700`: `write.lock`, `events.jsonl`, `index.jsonl`, `cache/state.json`, `<loop>.json`, `<loop>.context.json`).

## `bf.yaml` schema {#bf-yaml-schema}

Published at [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json):

```yaml
version: 15
name: my-brain
brains:
  team-brain: ../team-brain
timezone: UTC
env: {}
loops: {}
watch:
  interval: 5s
  debounce: 300ms
  concurrency: 4
```

## Published JSON Schemas

- [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json): `bf.yaml` (`Config` — unified `loops:` and `watch`).
- [`record.schema.json`](https://fmind.github.io/brain-framework/record.schema.json): `memories/**/*.json` (`Record` — frontmatter + `headings`, keyed `attrs`, `elements`, `links`, `tasks`, `text`, `sha256`).
- [`concepts.schema.json`](https://fmind.github.io/brain-framework/concepts.schema.json): `schemas/concepts.json` (`Concept` — user-defined concept metadata).
- [`evals.schema.json`](https://fmind.github.io/brain-framework/evals.schema.json): `evals/*.yaml` (`Eval` — deterministic checks).
