# Operations

Orchestrate every deterministic sensor, compilation routine, git hook, continuous stream listener, and autonomous thinking bot under one unified `loops:` primitive in `bf.yaml` (`version: 16`), auto-load local `.env` overrides, supervise concurrently with `bf watch`, and preserve strict filesystem and process boundaries.

## Unified `loops:` orchestration {#ingest-sensor-records}

Sensors (`examples/sensors/`: `git-history.py`, `github-issues.py`, `gmail-headers.py`, `local-documents.py`, and circular `agent-telemetry.py`), routines, continuous listeners (`on: daemon`), and LLM agent loops (`agy`, `claude`, custom wrappers) all declare the same `loops:` entry in `bf.yaml`:

```yaml
# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json
version: 16
name: my-brain
description: Personal knowledge base and operational sensors.
timezone: UTC
env:
  GOOGLE_WORKSPACE_CLI_CONFIG_DIR: "{{home}}/.config/gws"
loops:
  github-issues:
    description: Collect GitHub issues incrementally into memories/github-issues/.
    on:
      schedules: 30m
    window:
      backfill: 1d
      overlap: 5m
    run: [python3, sensors/github-issues.py, acme/demo, "{{start}}", "{{end}}"]
    ingest: upsert
  build:
    description: Recompile Markdown notes on commit or after ingestion.
    on:
      events: records.ingested
      hooks: pre-commit
    run: [bf, build]
  chat:
    description: Stream incoming Google Chat messages as loop.message events.
    on: daemon
    run: [python3, loops/google-chat.py, listen]
    emit: loop.message
  assistant:
    description: Triage direct messages and run scheduled work digests.
    on:
      schedules:
        - 1h
        - "30 7 * * 1"
      events:
        kind: loop.message
        source: chat
        filter: ".data.direct == true"
    run: [claude, -p, "Prompt: prompts/work.md Context: {{context}}"]
    emit: loop.event
    timeout: 15m
  security-fixer:
    description: Investigate security alerts and save structured findings into memories/security-fixer/.
    on:
      events:
        kind: security.alert
        source: assistant
    run: [claude, -p, "Prompt: prompts/security.md Context: {{context}}"]
    ingest: snapshot
    timeout: 1h
  pre-commit-check:
    description: Validate schemas, notes, records, and links before commit.
    on:
      hooks: pre-commit
    run: [bf, check, --validate]
watch:
  interval: 5s
  concurrency: 4
```

- **Time is a type of event (`on:`)**: set `on: daemon` for continuous supervision, omit `on:` for manual-only loops, or declare `on: {schedules?, events?, hooks?}` where each key accepts a single value or a list: `schedules` (whole-second duration `30s`/`15m`/`1h30m`/`1d`/`1w`, `@macro`, or 5-field cron evaluated in `timezone`), `events` (kind string shorthand like `records.ingested` or `{kind?, source?, filter?}` with isolated `jq` `filter`), and Git `hooks` (`pre-commit`, `pre-push`). Reactive `event`/`hook` runs preserve schedule anchors, and per-loop `event_seq` cursors self-heal if `.bf/events.jsonl` is reset.
- **Time windows (`window:`)**: optional `window: {backfill: 1d, overlap: 0}` controls `{{start}}` / `BF_START` (`now - backfill` on the first run before any success; `last_anchor - overlap` on later runs to catch late-arriving updates).
- **Continuous & triggered loops (`on: daemon`, `emit:`, `bf emit`)**: `on: daemon` supervises long-running loops with exponential restart backoff (after `>= 30s` of sustained uptime, `consecutive_failures`, `error`, and backoff reset and `last_success` updates automatically) and forbids cycle-only keys (`window`, `ingest`, `timeout`). Set `emit: <kind>` (e.g. `emit: loop.event` or `emit: loop.message`) on **either** a daemon loop (streamed line-by-line) or a triggered cycle loop (parsed from stdout JSONL on exit `0`) to append deduplicated events to `.bf/events.jsonl` (JSON `"kind"` overrides the default), or publish mid-run with `bf emit KIND --source NAME --data '{...}'`. Triggered loops automatically emit `loop.succeeded` / `loop.failed`, `bf build` emits `notes.built`, and `bf ingest` emits `records.ingested`.
- **Machine outputs belong in `memories/` (`ingest:`)**: `ingest: upsert` or `ingest: snapshot` defaults the target source to the loop name (`memories/<loop>/`), and `ingest: {source: <name>, mode: upsert|snapshot}` overrides it when multiple loops write one source. Each source is validated automatically by convention against `schemas/record.json` (or built-in `record`) plus optional `schemas/<source>.json`. `ingest:` and `emit:` are mutually exclusive; `actions/` stays dedicated to authored workspaces (`bf new action`).
- **Bounded concurrency, state hygiene, & singleton supervision (`watch.concurrency`)**: `bf run --due` runs due loops concurrently up to `watch.concurrency` (`1..32`, default `4`), while explicit `bf run LOOP ...` and `--hook` stay sequential; use `bf run --reset LOOP...` to clear stale state for a loop and `bf status --prune` to delete orphaned `.bf/<loop>.json` files and stale cache shards. `bf watch` holds an exclusive `.bf/watch.lock` (refusing a second supervisor on the same brain), debounces file edits (`300ms`), polls schedules every `watch.interval` (`5s` default), and dispatches due/event loops onto a non-blocking worker pool with per-loop single-flight so a long thinking loop never blocks fast sensors, chat listeners, or incremental note rebuilds.
- **Explicit `env:` map + `.env` overrides**: environment merges in order: allowlisted host env < top-level `bf.yaml env:` (`{{brain}}`, `{{home}}`, `{{state}}`) < per-loop `loops.<name>.env:` < optional gitignored `0600` `.env` (owned by `geteuid()`) < protected `BF_*` (`BF_BRAIN`, `BF_STATE_DIR`, `BF_START`, `BF_END`, `BF_TRIGGER`, `LC_ALL`, `LANG`). Both `{{state}}` and `BF_STATE_DIR` resolve to `<brain>/.bf/<loop>`.
- **Pure JSON storage, `.forgotten`, & snapshot guard**: `bf ingest` writes `memories/<source>/<sha256(id)>.json` atomically with batched directory `fsync`, honors `memories/<source>/.forgotten`, skips snapshot pruning when `incomplete` is set, and refuses to empty a non-empty source or drop >50% of 10+ records unless `bf ingest --allow-removal` is passed explicitly on the CLI.

## Limits and safeguards {#limits-and-safeguards}

- **Confined `O_NOFOLLOW` access**: `bf` opens every brain file (including `.env`) through `O_NOFOLLOW` directory descriptors; symlinks inside managed folders are rejected (`actions/*/inputs/` and `outputs/` are not traversed during note compilation or validation).
- **Bounded sizes**: `bf.yaml` and `schemas/*.json` cap at `1 MiB` (`.env` caps at `64 KiB`); OKF notes cap at `4 MiB`; `bf ingest` batches and emitted loop outputs cap at `16 MiB`; scanned trees cap at `100,000` entries per source (`1,000,000` total across `memories/`) and `64` levels.

## Privacy and security boundaries {#privacy-and-security-boundaries}

- **No built-in LLM or provider SDK**: `bf` never calls model APIs directly; `run` commands execute with literal `argv` (`shell=False`), allowlisted environments merged with `env:` and `.env` (never overriding `BF_BRAIN`, `BF_STATE_DIR`, `BF_START`, `BF_END`, `BF_TRIGGER`, `LC_ALL`, or `LANG`), dedicated process groups, and `.bf/<name>.context.json` (`0600`). `jq` queries and event filters run from `/` with module imports and environment access disabled.
- **Private `.bf/` & `logs/` state and secret redaction**: `.bf/` (`write.lock`, `watch.lock`, `events.jsonl`, `cache/{index,validate}/<source>.json`, `<loop>.json`) and `logs/` (`0600` plain JSON line `*.log` files) are enforced as `0700` owned by `geteuid()`, `bf mcp` redacts host paths and secrets, and diagnostics redact common tokens/keys and strip control characters.
