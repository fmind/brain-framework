# Reference

Complete contract lookup for Brain Framework (`bf`), flat brain layout, `bf.yaml` (`version: 16`), pure JSON `memories/**/*.json` schemas, `.bf/`, plain JSON line logs, and exit codes.

## CLI commands

Every command writes one compact UTF-8 JSON object to `stdout` (except `bf --version`, `--help`, `bf mcp`, `--format jsonl|tsv`, and live `bf watch`) and single-line `bf: ...` diagnostics to `stderr`. `--brain PATH` may appear before or after any subcommand except `bf init` and `bf schema`.

- `bf --version`: print the installed package version.
- `bf init PATH [--name NAME] [--description TEXT]`: scaffold a minimal `version: 16` brain (`bf.yaml`, `.gitignore`, `AGENTS.md`, `concepts/welcome.md`, `evals/smoke.yaml`, `skills/bf/SKILL.md` linked at `.agents/skills/bf`, and compiled `memories/concepts/welcome.json`).
- `bf build [--force] [--brain PATH]`: compile `projects/**/*.md`, `concepts/**/*.md`, and `actions/*/ACTION.md` into `memories/{projects,concepts,actions}/<slug>.json` and update authored `.bf/cache/index/` shards without scanning sensor sources.
- `bf ingest SOURCE [--input FILE] [--mode upsert|snapshot] [--allow-removal] [--dry-run] [--brain PATH]`: validate records against the bundled `record` schema + optional `schemas/<source>.json` and write `memories/<source>/<sha256(id)>.json` (rejects interactive TTY stdin when `--input -`).
- `bf read TARGET [--outline | --lines START[:COUNT] | --raw | --full] [--fields F,...] [--max-bytes N] [--brain PATH]`: resolve an OKF note (or `#anchor`, `START:` reads to EOF), `source:id`, JSON memory path, alias, or cross-brain `bf://<host>/<path>`. Default note reads, `#anchor`, `--raw`, `--lines`, and plain files return body text under `"text"` (`--full` and sensor records return `"record"`).
- `bf query [PATTERN] [--jq EXPR] [--kind note|memory] [-s/--scope PATH] [--fields F,...] [-n/--limit N] [--format json|jsonl|tsv] [--brain PATH]`: run `rg`, `jq`, or combined `rg` + `jq` over `memories/**/*.json` (`--limit` `1..5000`, default `20`).
- `bf review [-n/--limit N] [--all] [--fields F,...] [--format json|jsonl|tsv] [--brain PATH]`: report `stale` notes (`newer_evidence`, `due`, `future_updated`), open `draft` `actions_open`, and `total_tasks_open` via per-source `.bf/cache/index/<source>.json` shards (`--limit` default `20`; `jsonl`/`tsv` rows include `"section": "stale"|"action_open"` unless omitted by `--fields`).
- `bf check [TARGET] [--validate | --eval] [-n/--limit N] [--all] [--strict] [--force] [--brain PATH]`: run incremental `build`, read-only schema/link validation, and deterministic evals (`--validate` runs read-only validation only; `--eval [TARGET]` runs eval suites only; `--limit` default `50`; snapshot/forgotten missing targets and outdated `skills/bf/SKILL.md` warn unless `--strict`).
- `bf new concept|project|action SLUG [--title TITLE] [--type TYPE] [--status STATUS] [--brain PATH]`: scaffold a valid note via `Store` and rebuild compiled memories.
- `bf schema [bf|record|concepts|evals]`: print one or all bundled Draft 2020-12 JSON Schemas (no brain required).
- `bf run [LOOP ...] [--due | --hook pre-commit|pre-push | --reset] [--dry-run] [--brain PATH] [-- EXTRA_ARGS...]`: execute named cycle loops, rebuild dirty notes and run due/event loops concurrently up to `watch.concurrency` (`--due`), run sequential Git hooks (`--hook`), or clear saved state/context for named loops (`--reset LOOP...`).
- `bf emit KIND [--source NAME] [--data JSON] [--dedupe] [--brain PATH]`: append a custom event directly to `.bf/events.jsonl` and `logs/events.log`.
- `bf status [--all] [--fields F,...] [--check] [--prune] [--brain PATH]`: inspect `"inside_brain"`, optional `"description"`, compact `counts` (`notes`, `records`, `sources`, `loops`), failing `attention` loops, `orphaned_states`, `skill_outdated`, and optional `loops` (`--all`). Pass `--prune` to delete orphaned loop state files and stale/legacy `.bf/` caches.
- `bf watch [--json] [--brain PATH]`: run the `watchfiles` supervisor, daemon loops, non-blocking bounded worker pool, and Rich TUI dashboard (auto-emits JSON tick lines when non-TTY or `--json`).
- `bf mcp [--brain PATH]`: run the strictly read-only `stdio` MCP server (`bf_status`, `bf_review`, `bf_read(..., raw, full)`, `bf_query(pattern, jq, kind, scope, fields, limit)` with `limit` `1..200`, `bf_check`; never mutates brain files; cross-brain links require `bf.yaml` `brains:`).

Brain selection resolves `--brain PATH` -> `BF_BRAIN` -> nearest enclosing `bf.yaml` -> `DEFAULT_BRAIN_PATH` (zero global config).

## Exit codes

- `0`: success (`ok: true` on `build`, `query`, `review`, `check`, `new`, `run`, `status --check`, `emit`).
- `1`: validation failure, failed loop/eval, missing/corrupt files, schema violation, or lock contention.
- `2`: invalid CLI flags, conflicting selectors, running an `on: daemon` loop via `bf run`, TTY stdin on `bf ingest`, or malformed argument syntax (`bf: invalid input: ...`).
- `130`: interrupted (`SIGINT`).

## Flat brain layout

`projects/`, `concepts/`, `actions/`, `memories/`, `evals/`, `skills/`, optional `schemas/` (`concepts.json` or `<source>.json` to override built-in schemas), optional operational folders created as needed (`sensors/`, `routines/`, `loops/`, `prompts/`, `settings/`, `docs/`), optional gitignored `.env`, plus private `logs/` (`0700`: `0600` plain JSON line `*.log`) and `.bf/` (`0700`: `write.lock`, `watch.lock`, `events.jsonl`, `cache/{index,validate}/<source>.json`, `<loop>.json`, `<loop>.context.json`).

## `bf.yaml` schema {#bf-yaml-schema}

Published at [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json):

```yaml
# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json
version: 16
name: my-brain
description: Short summary for humans and AI agents.
brains:
  team-brain: ../team-brain
timezone: UTC
env: {}
loops: {}
watch:
  interval: 5s
  concurrency: 4
```

## Published JSON Schemas

- [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json): `bf.yaml` (`Config` — unified `loops:` and `watch`).
- [`record.schema.json`](https://fmind.github.io/brain-framework/record.schema.json): `memories/**/*.json` (`Record` — frontmatter + `headings`, keyed `anchors`, `elements`, `links`, `tasks`, `text`, `sha256`).
- [`concepts.schema.json`](https://fmind.github.io/brain-framework/concepts.schema.json): default `concepts` schema (override optionally via `schemas/concepts.json`).
- [`evals.schema.json`](https://fmind.github.io/brain-framework/evals.schema.json): `evals/*.yaml` (`Eval`, `version: 1` — deterministic checks).
