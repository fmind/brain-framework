# Reference

Complete contract lookup for Brain Framework (`bf`), 3x3 brain layout, `bf.yaml` (`version: 17`), JSONL records (`memories/`, `usages/`, `logs/`), required JSON Schemas, and exit codes.

## CLI commands

Every command writes one compact UTF-8 JSON object with `"ok"` to `stdout` (except `bf --version`, `--help`, `bf schema`, `--format jsonl`, `--format text`, and live `bf watch`) and single-line `bf: ...` diagnostics to `stderr`. `--brain PATH` may appear before or after any subcommand except `bf init` and `bf schema`.

**Strictly read-only commands (never mutate disk):**

- `bf --version`: print the installed package version.
- `bf status [--all] [--format json|text] [--check] [--brain PATH]`: inspect `"inside_brain"`, compact `counts` (`notes`, `records`, `usages`, `sources`, `loops`), failing `attention` loops, `orphaned_states`, and optional `loops` (`--all`). Pass `--format text` for a bird's-eye overview table or `--check` to exit `1` on failures.
- `bf review [-n/--limit N | --all] [--format json|jsonl] [--brain PATH]`: scan `memories/*.jsonl` and report `stale` notes (`newer_evidence`, `due`, `future_updated`) and `total_tasks_open` (`--limit` default `20`).
- `bf read TARGET [--raw] [--max-bytes N] [--brain PATH]`: resolve a note, `#anchor` heading section, `source:id` record (in `memories/` or `usages/`), alias, or declared cross-brain `bf://<host>/<ref>`.
- `bf query [PATTERN] [--jq EXPR] [--kind note|memory|usage] [-s/--scope PATH] [-n/--limit N] [--format json|jsonl] [--brain PATH]`: run `rg`, `jq`, combined `rg` + `jq`, or compact summary listing over `memories/*.jsonl` (or `usages/*.jsonl` via `--kind usage` / `--scope usages`; `--limit` `1..5000`, default `20`).
- `bf check [-n/--limit N | --all] [--strict] [--brain PATH]`: strictly read-only validation of `bf.yaml`, `.env`, schemas, compiled-note sync, `memories/*.jsonl`, `usages/*.jsonl`, enabled `loops:` (scripts, ingest schemas, `jq` filters), links, `#anchor` headings, and declared cross-brain targets (`--limit` default `50`).
- `bf schema [bf|record]`: print one or both bundled Draft 2020-12 JSON Schemas (no brain required).

**Explicit write commands:**

- `bf init PATH [--name NAME] [--description TEXT]`: scaffold a `version: 17` brain with 3x3 folders (`concepts/`, `projects/`, `actions/`, `sensors/`, `routines/`, `agents/`, `memories/`, `usages/`, `logs/`), starter schemas (`schemas/{concepts,projects,actions,usages}.json`), `evals/smoke.py`, and compiled `memories/concepts.jsonl`.
- `bf build [--brain PATH]`: compile Markdown notes across configured `notes:` folders into `memories/<dir>.jsonl` (`kind: "note"`), preserving prior output on partial failure.
- `bf ingest SOURCE [--to memories|usages] [--input FILE] [--mode upsert|snapshot] [--allow-removal] [--dry-run] [--brain PATH]`: validate records against bundled `record` + required `schemas/<source>.json` (or `schemas/usages.json`), write `<target>/<source>.jsonl`, and emit `records.ingested`.
- `bf run [LOOP ...] [--all | --due | --reset | --prune] [--dry-run] [--brain PATH] [-- EXTRA_ARGS...]`: execute named/glob, `--all`, or `--due` cycle loops, reset `.bf/<loop>.json` state (`--reset`), or prune orphaned `.bf/<loop>.json` files (`--prune`; `--dry-run` works across all modes).
- `bf emit KIND [--source NAME] [--data JSON|-] [--dedupe] [--brain PATH]`: append a custom event to `logs/events.jsonl` (`--data -` reads JSON from non-TTY stdin).
- `bf watch [--format auto|text|jsonl] [--brain PATH]`: supervise daemon loops, note rebuilds, and concurrent cycle loops; prints bird's-eye status + live text lines on TTY, or structured JSONL when piped / `--format jsonl`.

Brain selection resolves `--brain PATH` -> nearest enclosing `bf.yaml` -> `BF_BRAIN` -> `$BRAIN_LOCATION` -> `~/brain` (zero global config).

## Exit codes

- `0`: success (`ok: true`).
- `1`: validation failure, missing required schema, failed loop, missing/corrupt files, schema violation, or lock contention.
- `2`: invalid CLI flags, conflicting selectors, running a `when: daemon` loop via `bf run`, TTY stdin on `bf ingest`/`bf emit --data -`, undeclared cross-brain `bf://` host, or malformed argument syntax.
- `130`: interrupted (`SIGINT`).

## 3x3 brain layout

- **Notes (`notes:`):** `concepts/`, `projects/`, `actions/` (flat `<slug>.md` or `<slug>/index.md` bundles).
- **Loops (`loops:`):** `sensors/` (auto-ingested collectors), `routines/` (maintenance/reports/daemons), `agents/` (LLM/harness tasks).
- **Records (JSONL):** `memories/` (`<source>.jsonl` + `.forgotten`), `usages/` (`<source>.jsonl` + `.forgotten`), `logs/` (`0700` private: `0600` `logs/<loop>.jsonl` + `logs/events.jsonl`).
- **Supporting & runtime:** `schemas/` (`<source>.json`, `usages.json`), `evals/` (user Python/pytest evals), optional `clients/` & `prompts/` (convention-only), optional gitignored `0600` `.env`, and disposable `0700` `.bf/` (`write.lock`, `watch.lock`, `<loop>.json`, `<loop>/`).

## `bf.yaml` schema {#bf-yaml-schema}

Published at [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json):

```yaml
# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json
version: 17
name: my-brain
description: Short summary for humans and AI agents.
notes: [concepts, projects, actions]
brains:
  team-brain: ../team-brain
timezone: UTC
concurrency: 4
env: {}
defaults:
  timeout: 5m
  window:
    backfill: 1d
    overlap: 0
loops: {}
```

## Published JSON Schemas

- [`bf.schema.json`](https://fmind.github.io/brain-framework/bf.schema.json): `bf.yaml` (`Config`: `notes`, `concurrency`, `env`, `defaults`, composable `loops` with `when` and `ingest` targets).
- [`record.schema.json`](https://fmind.github.io/brain-framework/record.schema.json): `memories/<source>.jsonl` & `usages/<source>.jsonl` (`Record`: uniform `kind: "note"|"memory"|"usage"`, universal `ref`, frontmatter/attributes, `headings`, `links`, `tasks`, `text`, `sha256`).
