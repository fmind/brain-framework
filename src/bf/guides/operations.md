# Operations

Orchestrate deterministic sensors (`sensors/`), maintenance routines (`routines/`), coding-harness agents (`agents/`), and continuous stream listeners (`when: daemon`) under one minimal `loops:` primitive in `bf.yaml` (`version: 17`), lean on shell/Python folder conventions plus the two-stream `stdout`/`stderr` JSONL contract, supervise concurrently with `bf watch`, and preserve strict filesystem and process boundaries.

## Composable `loops:` orchestration {#ingest-sensor-records}

Sensors (`examples/sensors/`: `git-history.py`, `github-issues.py`, `gmail-headers.py`, `local-documents.py`, and circular `agent-telemetry.py`), routines, continuous listeners (`when: daemon`), and coding-harness agents (`agy`, `claude`, `copilot`) share one minimal `loops:` model in `bf.yaml`:

```yaml
# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json
version: 17
name: my-brain
description: Personal knowledge base and operational sensors.
timezone: UTC
concurrency: 4
defaults:
  timeout: 5m
loops:
  github-issues:
    description: Collect GitHub issues incrementally into memories/github-issues.jsonl.
    when:
      - every: 30m
        overlap: 5m
      - every: 1d
        backfill: 7d
        overlap: 7d
  agent-telemetry:
    description: Collect harness token/prompt usage into usages/agent-telemetry.jsonl.
    when: 1h
    run: [python3, sensors/agent-telemetry.py, agy, ~/.gemini/sessions]
    ingest:
      target: usages
  build:
    description: Recompile Markdown notes after ingestion.
    when:
      event: records.ingested
    run: [bf, build]
  chat:
    description: Stream incoming chat messages as loop.message events.
    when: daemon
    run: [python3, routines/local.py, listen]
    emit: loop.message
  triage:
    description: Triage incoming messages with an AI coding harness (agents/triage.sh).
    when:
      - cron: "30 7 * * 1"
        backfill: 1w
      - event:
          kind: loop.message
          source: chat
          filter: ".data.direct == true"
    emit: loop.event
```

- **Convention-first `sensors/`, `routines/`, & `agents/`**: omit `run:` when a matching script exists at `<dir>/<name>`, `<dir>/<name>.sh`, or `<dir>/<name>.py` (ambiguous duplicates fail closed). Executable scripts run directly; non-executable `.sh` runs via `bash` and `.py` via Python. Any non-daemon, non-emitting loop under `sensors/` (including interpreter-prefixed `run: [bash, sensors/foo.sh]`) auto-implies `ingest: upsert` into `memories/<name>.jsonl` unless `ingest:` overrides it (`snapshot` or `{source?, target?: memories|usages, mode?}`). Scripts under `agents/` default to a `15m` timeout unless overridden. Existing CLIs (`gh`, `gws`, `acli`, `gcloud`) come first; put ad-hoc API scripts in `clients/` and reusable LLM prompts in `prompts/`.
- **UNIX two-stream contract + `BF_*` env & `stdin`**:
  - **`env` & `stdin`**: every loop receives `BF_BRAIN`, `BF_LOOP`, `BF_STATE` (`<brain>/.bf/<loop>`), `BF_START`, `BF_END`, `BF_TRIGGER`, and `BF_RUN`. Cycle loops receive one compact JSON context object on `stdin` (`{"run", "trigger", "loop", "start", "end", "events"}`); daemons receive `/dev/null`.
  - **`stdout` (data plane)**: JSONL records for `ingest:` or JSONL events for `emit:`.
  - **`stderr` (observability plane)**: structured JSONL (`structlog` / JSON lines with `time`/`timestamp`, `severity`/`level`, `message`/`event`, plus extra fields) or plain text, secret-redacted and appended live to `logs/<loop>.jsonl` for both cycle loops and daemons (including on graceful stop).
- **Uniform `when:` triggers with per-trigger windows**: set `when: daemon` for continuous supervision, omit `when:` for manual loops, or pass a trigger or list of triggers (`1h`, `@daily`, `30 7 * * 1`, `{every, backfill?, overlap?}`, `{cron, backfill?, overlap?}`, `{event}`). Self-sourced `loop.succeeded`/`loop.failed` events are suppressed unless `source:` explicitly names the loop, and `bf watch` applies a per-loop event rate guard (`10`/min) against runaway cycles.
- **Single event bus (`logs/events.jsonl`)**: `emit: <kind>`, `bf emit KIND [--data JSON|-]`, `bf build` (`notes.built`), `bf ingest` (`records.ingested`), and triggered loops (`loop.succeeded` / `loop.failed`) append deduplicated events to `logs/events.jsonl` (`0600` private, compacted at `1000` lines / `8 MiB`).
- **Supervision & bird's-eye visibility (`bf watch` & `bf status --format text`)**: `bf watch` holds `.bf/watch.lock`, prints a bird's-eye status table on startup in TTY mode followed by clean live event lines (zero TUI cursor hijacking), and automatically emits structured JSONL when piped or given `--format jsonl`. Run `bf status --format text` anytime (or `tail -f logs/<loop>.jsonl`) to check live health.
- **Flat `env:` precedence**: `host allowlist < bf.yaml env: < loops.<name>.env: < optional 0600 .env < protected BF_*`.
- **JSONL storage, `.forgotten`, & snapshot guard**: `bf ingest [--to memories|usages] SOURCE` writes `<target>/<source>.jsonl` atomically (deleting empty files for DuckDB compatibility), honors `<target>/<source>.forgotten`, skips snapshot pruning when `incomplete` is set, and refuses to empty a non-empty source or drop >50% of 10+ records without `--allow-removal`.

## Limits and safeguards {#limits-and-safeguards}

- **Confined `O_NOFOLLOW` access**: `bf` opens every brain file through `O_NOFOLLOW` directory descriptors; symlinks inside managed folders are rejected, and non-`index.md` files/subdirectories inside bundle notes are never traversed.
- **Bounded sizes**: `bf.yaml` and `schemas/*.json` cap at `1 MiB` (`.env` caps at `64 KiB`); Markdown notes cap at `4 MiB`; `bf ingest` batches cap at `16 MiB`; individual JSONL files cap at `256 MiB` (sweet spot `<= 20k` records/source for single-file upserts); scanned trees cap at `100,000` entries per source (`1,000,000` total records) and `64` directory levels.

## Privacy and security boundaries {#privacy-and-security-boundaries}

- **No built-in LLM or provider SDK**: `bf` never calls model APIs directly; `run` commands execute with literal `argv` (`shell=False`), allowlisted environments, dedicated process groups, and JSON context on `stdin`. `jq` queries and event filters run from `/` with module imports and `$ENV` disabled.
- **Private `.bf/` & `logs/`**: `.bf/` (`0700`, disposable locks `write.lock`/`watch.lock` and per-loop state `<loop>.json`/`<loop>/`) and `logs/` (`0700`, `0600` JSONL `logs/<loop>.jsonl` and `logs/events.jsonl`, excluded from `bf read`, `bf query`, and note links) enforce `geteuid()` ownership, and diagnostics redact common secrets and strip control characters.
