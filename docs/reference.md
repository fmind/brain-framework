---
icon: lucide/book-open
description: Complete contract lookup for bf CLI commands, exit codes, 3x3 brain layout, bf.yaml v19, and published JSON Schemas.
---

# Reference

Complete contract lookup for Brain Framework (`bf`), 3x3 brain layout, `bf.yaml` (`version: 19`), JSONL records (`memories/`, `logs/`), disposable `.bf/cache/index.sqlite3`, optional domain JSON Schemas, and exit codes.

## CLI commands

Every command writes one compact UTF-8 JSON object with `"ok"` to `stdout` (except `bf --version`, `--help`, `bf schema`, `bf read --format raw`, `--format jsonl`, `--format text`, and live `bf watch`) and single-line `bf: ...` diagnostics to `stderr`. `--brain PATH` may appear before or after any subcommand except `bf init` and `bf schema`.

**Strictly read-only commands (never mutate disk):**

- `bf --version`: print the installed package version.
- `bf status [--loops] [--check] [--format json|text] [--brain PATH]`: inspect `"inside_brain"`, compact `counts` (`notes`, `memories`, `sources`, `loops`), top-level `events_seq`, optional `pending_timers` and `paused` loops, `index_stale`, failing/crashed/lagging/degraded/overdue `attention` loops, structured `orphans` (`{states, dirs, logs}`), `unbuilt_notes`, optional `scale_warnings`, and optional `loops` (`--loops`, including `pool`, `paused`, `paused_until`, `event_seq`, `events_missed`, `dead_lettered`, `runs`, `failures`). Exits `0` whenever readable; pass `--check` to exit `1` when `"ok": false`. Pass `--format text` for a calm overview table (`watch -n 2 bf status --format text`).
- `bf events [--since SEQ | --timers] [--kind KIND] [--source NAME] [-n/--limit N | --all] [--format json|jsonl|text] [--brain PATH]`: inspect recent events in `.bf/state/events.jsonl` or pending delayed `--timers` in `.bf/state/timers.jsonl` (`--limit` `1..5000`, default `20`; `--since SEQ` paginates forward for `seq > SEQ`, including `0`; without `--since`, returns the newest matching tail).
- `bf review [-n/--limit N | --all] [--format json|jsonl|text] [--brain PATH]`: report `stale` notes (`newer_evidence`, `due`, `future_updated`) and `total_tasks_open` (`--limit` `1..5000`, default `20`).
- `bf read TARGET [--format json|raw] [--max-bytes N] [--brain PATH]`: resolve a note, `#anchor` heading section, `source:id` record, entity/alias (including `<alias>#anchor`), bundle attachment under `notes:`, or declared cross-brain `bf://<host>/<ref>`.
- `bf query [QUERY] [--kind note|memory] [-s/--scope PATH] [--type TYPE] [--status STATUS] [--tag TAG] [--since ISO] [--links-to TARGET] [--exclude SOURCE]... [--all-sources] [-n/--limit N | --all] [--format json|jsonl|text] [--brain PATH]`: ranked BM25 FTS5 + structured/backlink query over `.bf/cache/index.sqlite3` with read-only JSONL fallback (`--limit` `1..5000`, default `20`).
- `bf check [--notes] [-n/--limit N | --all] [--strict] [--format json|text] [--brain PATH]`: strictly read-only validation of `bf.yaml`, `.env`, optional `schemas/*.json`, compiled-note sync, `memories/*.jsonl`, enabled `loops:` (including explicit `run:` script paths), links, `#anchor` headings, and referenced cross-brain targets (`--notes` skips per-line memory JSONL validation; `--limit` `1..5000`, default `50`).
- `bf schema [bf|record]`: print one or both bundled Draft 2020-12 JSON Schemas (no brain required).

**Explicit write commands:**

- `bf init PATH [--name NAME] [--description TEXT]`: scaffold a `version: 19` brain with 3x3 folders (`concepts/`, `projects/`, `actions/`, `sensors/`, `routines/`, `agents/`, `memories/`, `logs/`), idiomatic starter note schemas (`schemas/{concepts,projects,actions}.json`), `evals/smoke.py`, compiled `memories/_notes.jsonl`, and `.bf/cache/index.sqlite3`.
- `bf build [--dry-run] [--brain PATH]`: incrementally compile authored Markdown notes across configured `notes:` folders into `memories/_notes.jsonl` (`kind: "note"`) and sync `.bf/cache/index.sqlite3`, reusing unchanged notes and preserving prior output on partial failure.
- `bf ingest SOURCE [--input FILE] [--mode upsert|snapshot] [--schema NAME] [--allow-removal] [--dry-run] [--brain PATH]`: validate records against bundled `record` + optional `schemas/<schema|source>.json`, write `memories/<source>.jsonl`, sync `.bf/cache/index.sqlite3`, and emit `records.ingested` (including bounded `added_refs`/`updated_refs`).
- `bf run [LOOP ...] [--all | --due] [--dry-run] [--brain PATH] [-- EXTRA_ARGS...]`: execute named/glob, `--all`, or `--due` cycle loops under per-loop `.bf/locks/loop-<name>.lock` files (honoring named concurrency pools and promoting due delayed timers on `--due`).
- `bf pause [LOOP ...] [--all] [--for DURATION] [--dry-run] [--brain PATH]`: pause named/glob loops (cycle or daemon) or `--all` indefinitely (or auto-resume after `--for DURATION`) without editing `bf.yaml`.
- `bf resume [LOOP ...] [--all] [--dry-run] [--brain PATH]`: resume paused named/glob loops or `--all`.
- `bf state [--brain PATH] reset [LOOP ...] [--all] [--dry-run] [--brain PATH]`: reset `.bf/state/<loop>.json` cursors (anchored to current `event_seq`) for named/glob loops or `--all`.
- `bf state [--brain PATH] prune [--dry-run] [--brain PATH]`: remove orphaned `.bf/state/<loop>.json`, `.bf/state/<loop>/`, unlocked `.bf/locks/loop-*.lock`, and `logs/<loop>.jsonl` for loops removed from `bf.yaml`.
- `bf emit KIND [--source NAME] [--data JSON|-] [--in DURATION | --at ISO] [--dedupe] [--dry-run] [--brain PATH]`: validate and append a custom event to `.bf/state/events.jsonl` (or schedule a delayed event in `.bf/state/timers.jsonl` with `--in`/`--at`; `--data -` reads JSON from non-TTY stdin).
- `bf watch [--format auto|text|jsonl] [--brain PATH]`: headless supervisor for daemon loops, delayed timer promotion, note rebuilds, and pool-bounded concurrent cycle loops (`auto` defaults to `text` on non-dumb TTY and `jsonl` when piped or `TERM=dumb`).

Brain selection resolves `--brain PATH` -> `BF_BRAIN` -> nearest enclosing `bf.yaml` -> `~/brain` (zero global config).

## Exit codes

- `0`: success (`ok: true`, or readable `bf status` without `--check`).
- `1`: validation failure, `bf status --check` with attention/problems, failed loop, missing/corrupt files, schema violation, or lock contention.
- `2`: invalid CLI flags, conflicting selectors, running a `when: daemon` loop via `bf run`, TTY stdin on `bf ingest`/`bf emit --data -`, undeclared cross-brain `bf://` host, or malformed argument syntax.
- `130` / `143`: interrupted by `SIGINT` (`130`) or `SIGTERM` (`143`).

## Brain layout

- **Notes (`notes:`):** `concepts/`, `projects/`, `actions/` (flat `<slug>.md` or `<slug>/index.md` bundles).
- **Loops (`loops:`):** `sensors/`, `routines/`, `agents/` (folder convention resolves script path only; declare `ingest:` explicitly on cycle loops that ingest `stdout` records—forbidden with `emit:` or `when: daemon`—and override `timeout:` from its `5m` default when needed).
- **Records (three JSONL streams across `memories/` and `logs/`):** `memories/_notes.jsonl`, `memories/<source>.jsonl` (plus `<source>.forgotten`), and private `logs/<loop>.jsonl` (`0700` dir, `0600` files).
- **Supporting & runtime:** optional `schemas/` (`<source>.json`), `evals/` (user Python/pytest evals), reserved `clients/` & `prompts/` (convention-only), optional gitignored `0600` `.env`, and private `0700` `.bf/` (`cache/index.sqlite3`, `state/<loop>.json`, `state/<loop>/`, `state/events.jsonl`, `state/timers.jsonl`, `watch.lock`, `write.lock`, `logs.lock`, `corpus.lock`, `locks/loop-*.lock`).

## Loop environment, placeholders, and event predicates

| Env var            | Placeholder   | Value                                                                  |
| ------------------ | ------------- | ---------------------------------------------------------------------- |
| `BF_BRAIN`         | `{{brain}}`   | Absolute brain root path                                               |
| `BF_LOOP`          | `{{loop}}`    | Loop name from `bf.yaml`                                               |
| `BF_STATE`         | `{{state}}`   | Private loop state directory (`<brain>/.bf/state/<loop>`)              |
| `BF_START`         | `{{start}}`   | Inclusive UTC window start ISO timestamp                               |
| `BF_END`           | `{{end}}`     | Exclusive UTC window end ISO timestamp                                 |
| `BF_TRIGGER`       | `{{trigger}}` | Trigger kind (`manual`, `schedule`, `event`, or `daemon`)              |
| `BF_RUN`           | `{{run}}`     | Unique run identifier (`<loop>-<epoch>-<hex>`)                         |
| `BF_EVENTS_MISSED` | —             | Set only when compacted events were skipped while the loop was lagging |

- **`where:` operators** (dot-path keys evaluated in pure Python against event objects): exact scalar (`data.direct: true`), `{in: [...]}`, `{exists: true|false}`, and `{prefix: "..."}`.
- **Built-in events** (`.bf/state/events.jsonl`): `notes.built` (`bf build`), `records.ingested` (`bf ingest`, including bounded `added_refs`/`updated_refs`), `loop.succeeded`, and `loop.failed`.

## `bf.yaml` schema {#bf-yaml-schema}

Published at [`bf.schema.json`](bf.schema.json):

```yaml
# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json
version: 19
name: my-brain
description: Short description for humans and AI agents.
notes: [concepts, projects, actions]
brains:
  team-brain: ../team-brain
search:
  exclude: []
timezone: UTC
concurrency: 4
env: {}
loops: {}
```

## Published JSON Schemas

- [`bf.schema.json`](bf.schema.json): `bf.yaml` (`Config`: `notes`, `brains`, `search.exclude`, `concurrency`, `env`, composable `loops` with per-loop `env_from`, `pool`, flat/windowed `when`, `debounce`, `window`, `ingest`, and `emit`).
- [`record.schema.json`](record.schema.json): `memories/_notes.jsonl` & `memories/<source>.jsonl` (`Record`: uniform `kind: "note"|"memory"`, universal `ref`, `ingested_at`, frontmatter/attributes, `headings`, `links`, `tasks`, `text`, `sha256`).
