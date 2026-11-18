# Operations

Orchestrate one-shot `jobs` and constant `services` declaratively in `bf.yaml` (`timezone`, `env`, `jobs`, `services`, `watch`), auto-load local `.env` overrides, validate schemas and Git hooks, and preserve strict filesystem and process boundaries.

## Ingest sensor records and orchestrate `jobs` and `services` {#ingest-sensor-records}

Sensors are standalone scripts (`examples/sensors/`: `git-history.py`, `github-issues.py`, `gmail-headers.py`, `local-documents.py`, and circular `agent-telemetry.py`) emitting a JSON array, `{"records": [...], "incomplete": {"missed": N}}` envelope, or JSON Lines. Pipe them into `bf ingest SOURCE` or declare them in `bf.yaml`:

```yaml
version: 14
name: my-brain
timezone: UTC
env:
  GOOGLE_WORKSPACE_CLI_CONFIG_DIR: "{{home}}/.config/gws"
jobs:
  github-issues:
    run: [python3, sensors/github-issues.py, acme/demo, "{{start}}", "{{end}}"]
    ingest:
      source: github-issues
      mode: upsert
    schedule: 30m
    lookback: 1d
    overlap: 5m
  build:
    run: [bf, build]
    events:
      - kind: records.ingested
  morning-brief:
    run: [python3, routines/morning-brief.py, "{{context}}"]
    schedule: "0 8 * * 1-5"
  assistant:
    run: [claude, -p, "Context file: {{context}}"]
    input: prompts/reactive.md
    events:
      - kind: service.message
        source: chat
        filter: ".data.direct == true"
  pre-commit-check:
    run: [bf, validate]
    hook: pre-commit
services:
  chat:
    run: [python3, services/google-chat.py, listen]
    emit: messages
watch:
  interval: 5s
  debounce: 300ms
```

- **Humanized durations & cron `schedule`**: `lookback`, `overlap`, `timeout`, `watch.interval`, and `watch.debounce` accept humanized strings (`300ms`, `30s`, `15m`, `1h30m`, `1d`, `1w`) or numeric seconds (note `stale_after` accepts humanized durations, integer days, or ISO dates); `schedule` accepts either durations or 5-field/`@macro` cron expressions evaluated in `timezone`.
- **Explicit `env:` map + `.env` overrides**: top-level `env` and unit `env:` define non-secret `KEY: "value"` settings (expanding `{{brain}}`, `{{home}}`, and `{{state}}`), while an optional gitignored `.env` in the brain root supplies local secrets or overrides even when `bf` runs from another folder via `--brain PATH`.
- **Generic `jobs` (`input:`, `ingest:`, `action:`)**: `jobs` run any one-shot command on schedules, Git hooks, or `.bf/events.jsonl` triggers (reactive `event`/`hook` runs preserve schedule anchors, and per-unit `event_seq` cursors self-heal if `.bf/events.jsonl` is reset). `input: prompts/foo.md` feeds the file verbatim on `stdin` and exposes its path as `{{input}}` (and `"input"` in `{{context}}`); `ingest:` accepts shorthand `ingest: foo` or `{source: foo, mode: snapshot, allow_removal: false}`; `action: {keep: N}` saves stdout as a dated `ACTION.md` retaining the newest `N`. Outbound replies and notifications run as normal `jobs` or scripts.
- **Constant `services` (`emit: none | events | messages`)**: `services.<name>.run` supervises constant daemons under `bf watch`, emitting custom events (`emit: events`) or inbound `service.message` events (`emit: messages`) from JSONL stdout lines into append-only `.bf/events.jsonl`, while bounded `structlog` JSON lines append to `logs/<name>.log` and `logs/events.log`.
- **Pure JSON storage & `.forgotten`**: `bf ingest` validates records against `schemas/record.json` + optional `schemas/<source>.json` and writes `memories/<source>/<sha256(id)>.json` atomically while honoring `memories/<source>/.forgotten`.
- **Snapshot removal guard & incomplete batches**: `--mode snapshot` skips pruning when `incomplete.missed > 0` and refuses >50% drops on 10+ saved records unless `--allow-removal` (`allow_removal: true`) is set.

## Limits and safeguards {#limits-and-safeguards}

- **Confined `O_NOFOLLOW` access**: `bf` opens every brain file (including `.env`) through `O_NOFOLLOW` directory descriptors; symlinks inside managed folders are rejected.
- **Bounded sizes**: `bf.yaml` and `schemas/*.json` cap at `1 MiB` (`.env` caps at `64 KiB`); OKF notes cap at `4 MiB`; `bf ingest` batches cap at `16 MiB`; scanned trees cap at `100,000` entries and `64` levels.

## Privacy and security boundaries {#privacy-and-security-boundaries}

- **No built-in LLM or provider SDK**: `bf` never calls model APIs directly; `run` commands execute with literal `argv` (`shell=False`), clean allowlisted environments merged with top-level `env`, unit `env:`, and brain-root `.env` (never overriding `BF_BRAIN`, `BF_STATE_DIR`, `BF_START`, `BF_END`, `BF_TRIGGER`, `LC_ALL`, or `LANG`), dedicated process groups, and `.bf/<name>.context.json` (`0600`). `jq` queries and event filters run from `/` with module imports and environment access disabled.
- **Private `.bf/` & `logs/` state and sanitized errors**: `.bf/` (`write.lock`, append-only `events.jsonl`, `<unit>.json`) and `logs/` (`0600` `*.log`) are enforced as `0700` owned by `geteuid()`, `bf mcp` redacts local filesystem roots, and diagnostics strip control/format characters and omit raw schema failure values.
