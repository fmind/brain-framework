# Operations

Orchestrate one-shot `jobs`, bidirectional `channels`, and constant `services` declaratively in `bf.yaml` (`timezone`, `env`, `jobs`, `channels`, `services`, `watch`), auto-load local `.env` overrides, validate schemas and Git hooks, and preserve strict filesystem and process boundaries.

## Ingest sensor records and orchestrate `jobs`, `channels`, and `services` {#ingest-sensor-records}

Sensors are standalone scripts (`examples/sensors/`: `git-history.py`, `github-issues.py`, `gmail-headers.py`, `local-documents.py`, and circular `agent-telemetry.py`) emitting a JSON array, `{"records": [...], "incomplete": {"missed": N}}` envelope, or JSON Lines. Pipe them into `bf ingest SOURCE` or declare them in `bf.yaml`:

```yaml
version: 13
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
    run: [claude, -p, "Context file: {{context}}"]
    input: prompts/morning-brief.md
    schedule: "0 8 * * 1-5"
    channel: chat
  assistant:
    run: [claude, -p, "Context file: {{context}}"]
    input: prompts/reactive.md
    events:
      - kind: channel.message
        source: chat
        filter: ".data.direct == true"
    channel: chat
  pre-commit-check:
    run: [bf, validate]
    hook: pre-commit
channels:
  chat:
    listen: [python3, channels/google-chat.py, listen]
    send: [python3, channels/google-chat.py, send]
    timeout: 1m
services:
  mcp:
    run: [bf, mcp, --transport, http, --port, "8765"]
    enabled: false
watch:
  interval: 5s
  debounce: 300ms
```

- **Humanized durations & cron `schedule`**: `lookback`, `overlap`, `timeout`, `watch.interval`, and `watch.debounce` accept humanized strings (`300ms`, `30s`, `15m`, `1h30m`, `1d`, `1w`) or numeric seconds (note `stale_after` accepts humanized durations, integer days, or ISO dates); `schedule` accepts either durations or 5-field/`@macro` cron expressions evaluated in `timezone`.
- **Explicit `env:` map + `.env` overrides**: top-level `env` and unit `env:` define non-secret `KEY: "value"` settings (expanding `{{brain}}`, `{{home}}`, and `{{state}}`), while an optional gitignored `.env` in the brain root supplies local secrets or overrides even when `bf` runs from another folder via `--brain PATH`.
- **Generic `jobs` (`input:`, `ingest:`, `action:`, `channel:`)**: `jobs` run any one-shot command on schedules, Git hooks, or `.bf/events.json` triggers (reactive `event`/`hook` runs preserve schedule anchors). `input: prompts/foo.md` feeds the file verbatim on `stdin` and exposes its path as `{{input}}` (and `"input"` in `{{context}}`); `ingest:` accepts shorthand `ingest: foo` or `{source: foo, mode: snapshot, allow_removal: false}`; `action: {keep: N}` saves stdout as a dated `ACTION.md` retaining the newest `N`; `channel: chat` posts stdout via `channels.chat.send`.
- **Bidirectional `channels` & constant `services`**: `channels.<name>.listen` streams inbound `channel.message` events and `channels.<name>.send` delivers outbound replies when `jobs.<name>.channel` runs (auto-threading `conversation` and `reply_to` from the triggering `channel.message` event). `services.<name>.run` supervises constant daemons (`bf watch`), while bounded `structlog` JSON lines append to `logs/<name>.log` and `logs/events.log`.
- **Pure JSON storage & `.forgotten`**: `bf ingest` validates records against `schemas/record.json` + optional `schemas/<source>.json` and writes `memories/<source>/<sha256(id)>.json` atomically while honoring `memories/<source>/.forgotten`.
- **Snapshot removal guard & incomplete batches**: `--mode snapshot` skips pruning when `incomplete.missed > 0` and refuses >50% drops on 10+ saved records unless `--allow-removal` (`allow_removal: true`) is set.

## Limits and safeguards {#limits-and-safeguards}

- **Confined `O_NOFOLLOW` access**: `bf` opens every brain file (including `.env`) through `O_NOFOLLOW` directory descriptors; symlinks inside managed folders are rejected.
- **Bounded sizes**: `bf.yaml` and `schemas/*.json` cap at `1 MiB` (`.env` caps at `64 KiB`); OKF notes cap at `4 MiB`; `bf ingest` batches cap at `16 MiB`; scanned trees cap at `100,000` entries and `64` levels.

## Privacy and security boundaries {#privacy-and-security-boundaries}

- **No built-in LLM or provider SDK**: `bf` never calls model APIs directly; `run`, `listen`, and `send` commands execute with literal `argv` (`shell=False`), clean allowlisted environments merged with top-level `env`, unit `env:`, and brain-root `.env` (never overriding `BF_BRAIN`, `BF_STATE_DIR`, `BF_START`, `BF_END`, `BF_TRIGGER`, `LC_ALL`, or `LANG`), dedicated process groups, and `.bf/<name>.context.json` (`0600`).
- **Private `.bf/` state & sanitized errors**: `.bf/` (`write.lock`, `events.json`, `<unit>.json`) is enforced as `0700` owned by `geteuid()`, `bf mcp` redacts local filesystem roots, and diagnostics strip control/format characters and omit raw schema failure values.
