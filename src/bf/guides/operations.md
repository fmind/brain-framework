# Operations

Orchestrate one-shot `jobs`, bidirectional `channels`, and constant `servers` declaratively in `bf.yaml` (`defaults`, `jobs`, `channels`, `servers`, `watch`), auto-load local `.env` overrides, validate schemas and Git hooks, and preserve strict filesystem and process boundaries.

## Ingest sensor records and orchestrate `jobs`, `channels`, and `servers` {#ingest-sensor-records}

Sensors are standalone scripts (`examples/sensors/`: `git-history.py`, `github-issues.py`, `gmail-headers.py`, `local-documents.py`, and circular `agent-telemetry.py`) emitting a JSON array, `{"records": [...], "incomplete": {"missed": N}}` envelope, or JSON Lines. Pipe them into `bf ingest SOURCE` or declare them in `bf.yaml`:

```yaml
version: 12
name: my-brain
defaults:
  timezone: UTC
  env:
    GOOGLE_WORKSPACE_CLI_CONFIG_DIR: "{{home}}/.config/gws"
jobs:
  github-issues:
    run: [python3, sensors/github-issues.py, acme/demo, "{{start}}", "{{end}}"]
    ingest:
      source: github-issues
      mode: upsert
    every: 30m
    lookback: 1d
    overlap: 5m
  build:
    builtin: build
    events:
      - kind: records.ingested
  morning-brief:
    run: [claude, -p, "Context file: {{context}}"]
    input: prompts/morning-brief.md
    at: "08:00"
    days: [mon, tue, wed, thu, fri]
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
    builtin: validate
    hook: pre-commit
channels:
  chat:
    listen: [python3, channels/google-chat.py, listen]
    send: [python3, channels/google-chat.py, send]
    timeout: 1m
servers:
  mcp:
    run: [bf, mcp, --transport, http, --port, "8765"]
    enabled: false
watch:
  interval: 5s
  debounce: 300ms
```

- **Humanized durations**: `every`, `lookback`, `overlap`, `timeout`, `watch.interval`, `watch.debounce`, and note `stale_after` accept humanized strings (`300ms`, `30s`, `15m`, `1h30m`, `1d`, `1w`) or numeric seconds.
- **Explicit `env:` map + `.env` overrides**: `defaults.env` and unit `env:` define non-secret `KEY: "value"` settings (expanding `{{brain}}`, `{{home}}`, and `{{state}}`), while an optional gitignored `.env` in the brain root supplies local secrets or overrides even when `bf` runs from another folder via `--brain PATH`.
- **Generic `jobs` (`input:`, `ingest:`, `channel:`)**: `jobs` run any one-shot command on schedules, Git hooks, or `logs/events.json` triggers (reactive `event`/`hook` runs preserve `every`/`at` schedule anchors). `input: prompts/foo.md` feeds the file verbatim on `stdin` and exposes its path as `{{input}}` (and `"input"` in `{{context}}`); `ingest:` accepts shorthand `ingest: foo` or `{source: foo, mode: snapshot, allow_removal: false}`; `channel: chat` posts stdout via `channels.chat.send` (forwarding `conversation` and `reply_to` on single same-channel `channel.message` triggers).
- **Bidirectional `channels` & constant `servers`**: `channels.<name>.listen` streams inbound `channel.message` events and `channels.<name>.send` delivers outbound messages (`bf post` auto-selects the channel when only one enabled channel defines `send`). `servers.<name>.run` supervises constant daemons (`bf watch`).
- **Pure JSON storage & `.forgotten`**: `bf ingest` validates records against `schemas/record.json` + optional `schemas/<source>.json` and writes `memories/<source>/<sha256(id)>.json` atomically while honoring `memories/<source>/.forgotten`.
- **Snapshot removal guard & incomplete batches**: `--mode snapshot` skips pruning when `incomplete.missed > 0` and refuses >50% drops on 10+ saved records unless `--allow-removal` (`allow_removal: true`) is set.

## Limits and safeguards {#limits-and-safeguards}

- **Confined `O_NOFOLLOW` access**: `bf` opens every brain file (including `.env`) through `O_NOFOLLOW` directory descriptors; symlinks inside managed folders are rejected.
- **Bounded sizes**: `bf.yaml` and `schemas/*.json` cap at `1 MiB` (`.env` caps at `64 KiB`); OKF notes cap at `4 MiB`; `bf ingest` batches cap at `16 MiB`; scanned trees cap at `100,000` entries and `64` levels.
- **Sandboxed DuckDB SQL**: `bf query --sql` rejects dot-commands, applies `SET memory_limit = '512MB';` before materializing `CREATE TEMP TABLE records` with explicit columns, and locks configuration with `SET enable_external_access = false; SET lock_configuration = true;` before running user SQL.

## Privacy and security boundaries {#privacy-and-security-boundaries}

- **No built-in LLM or provider SDK**: `bf` never calls model APIs directly; `run`, `listen`, and `send` commands execute with literal `argv` (`shell=False`), clean allowlisted environments merged with `defaults.env`, unit `env:`, and brain-root `.env` (never overriding `BF_BRAIN`, `BF_STATE_DIR`, `BF_START`, `BF_END`, `BF_TRIGGER`, `LC_ALL`, or `LANG`), dedicated process groups, and `.bf/state/<name>.context.json` (`0600`).
- **Private `.bf/` state & sanitized errors**: `.bf/` (`locks/`, `state/`) is enforced as `0700` owned by `geteuid()`, `bf mcp` redacts local filesystem roots, and diagnostics strip control/format characters and omit raw schema failure values.
