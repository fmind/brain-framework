# Operations

Orchestrate every deterministic sensor, compilation routine, git hook, continuous stream listener, and autonomous thinking bot under one unified `loops:` primitive in `bf.yaml` (`version: 15`), auto-load local `.env` overrides, supervise concurrently with `bf watch`, and preserve strict filesystem and process boundaries.

## Unified `loops:` orchestration {#ingest-sensor-records}

Sensors (`examples/sensors/`: `git-history.py`, `github-issues.py`, `gmail-headers.py`, `local-documents.py`, and circular `agent-telemetry.py`), routines, continuous listeners (`daemon: true`), and LLM agent loops (`agy`, `claude`, custom wrappers) all declare the same `loops:` entry in `bf.yaml`:

```yaml
version: 15
name: my-brain
timezone: UTC
env:
  GOOGLE_WORKSPACE_CLI_CONFIG_DIR: "{{home}}/.config/gws"
loops:
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
    hook: pre-commit
  chat:
    run: [python3, loops/google-chat.py, listen]
    daemon: true
    emit: loop.message
  assistant:
    run: [claude, -p, "Context file: {{context}}"]
    input: prompts/work.md
    schedule:
      - 1h
      - "30 7 * * 1"
    events:
      - kind: loop.message
        source: chat
        filter: ".data.direct == true"
    emit: true
    timeout: 15m
  security-fixer:
    run: [claude, -p, "Context file: {{context}}"]
    input: prompts/security.md
    events:
      - kind: security.alert
        source: assistant
    action:
      keep: 5
    timeout: 1h
  pre-commit-check:
    run: [bf, validate]
    hook: pre-commit
watch:
  interval: 5s
  debounce: 300ms
  concurrency: 4
```

- **Time is a type of event (`schedule` + `events` + `hook`)**: `schedule` accepts a single whole-second humanized duration (`30s`, `15m`, `1h30m`, `1d`, `1w`), `@macro`, 5-field cron expression, **or a list of schedules** evaluated in `timezone` — and combines freely with `events:` (`.bf/events.jsonl` rules with optional isolated `jq` `filter`) and git `hook:` (`pre-commit`, `pre-push`). Reactive `event`/`hook` runs preserve schedule anchors, and per-loop `event_seq` cursors self-heal if `.bf/events.jsonl` is reset.
- **Continuous & triggered loops (`daemon: true`, `emit:`, `bf emit`)**: set `daemon: true` for long-running supervised loops with exponential restart backoff (only sustained `>= 30s` uptime resets backoff). Set `emit: true` (default kind `loop.event`) or `emit: <kind>` (e.g. `emit: loop.message`) on **either** a daemon loop (streamed line-by-line) or a triggered cycle loop (parsed from stdout JSONL on exit `0`) to append deduplicated events to `.bf/events.jsonl` (JSON `"kind"` overrides the default), or publish mid-run with `bf emit KIND --source NAME --data '{...}'`. Triggered loops automatically emit `loop.succeeded` / `loop.failed`, `bf build` emits `notes.built`, and `bf ingest` emits `records.ingested`.
- **I/O options (`input:`, `ingest:`, `action:`)**: `input: prompts/foo.md` (cycle loops only) feeds the file on `stdin` and expands `{{input}}` (and `"input"` in `{{context}}`); `ingest:` accepts shorthand `ingest: foo` or `{source: foo, mode: snapshot, allow_removal: false}`; `action: {keep: N}` saves stdout as a dated `ACTION.md` retaining the newest `N`. Output modes (`ingest`, `action`, `emit: true|<kind>`) are mutually exclusive.
- **Bounded concurrency for many loops (`watch.concurrency`)**: `bf run --due` runs due loops concurrently up to `watch.concurrency` (`1..32`, default `4`), while explicit `bf run LOOP ...` and `--hook` stay sequential. `bf watch` dispatches due/event loops onto a non-blocking worker pool with per-loop single-flight so a 30-minute thinking loop never blocks fast sensors, chat listeners, or incremental note rebuilds.
- **Explicit `env:` map + `.env` overrides**: environment merges in order: allowlisted host env < top-level `bf.yaml env:` (`{{brain}}`, `{{home}}`, `{{state}}`) < optional gitignored `0600` `.env` (owned by `geteuid()`) < per-loop `loops.<name>.env:` < protected `BF_*` (`BF_BRAIN`, `BF_STATE_DIR`, `BF_START`, `BF_END`, `BF_TRIGGER`, `LC_ALL`, `LANG`).
- **Pure JSON storage, `.forgotten`, & snapshot guard**: `bf ingest` writes `memories/<source>/<sha256(id)>.json` atomically with batched directory `fsync`, honors `memories/<source>/.forgotten`, skips snapshot pruning when `incomplete` is set, and refuses to empty a non-empty source or drop >50% of 10+ records unless `--allow-removal` (`allow_removal: true`) is passed.

## Limits and safeguards {#limits-and-safeguards}

- **Confined `O_NOFOLLOW` access**: `bf` opens every brain file (including `.env`) through `O_NOFOLLOW` directory descriptors; symlinks inside managed folders are rejected.
- **Bounded sizes**: `bf.yaml` and `schemas/*.json` cap at `1 MiB` (`.env` caps at `64 KiB`); OKF notes cap at `4 MiB`; `bf ingest` batches and emitted loop outputs cap at `16 MiB`; scanned trees cap at `100,000` entries and `64` levels.

## Privacy and security boundaries {#privacy-and-security-boundaries}

- **No built-in LLM or provider SDK**: `bf` never calls model APIs directly; `run` commands execute with literal `argv` (`shell=False`), allowlisted environments merged with `env:` and `.env` (never overriding `BF_BRAIN`, `BF_STATE_DIR`, `BF_START`, `BF_END`, `BF_TRIGGER`, `LC_ALL`, or `LANG`), dedicated process groups, and `.bf/<name>.context.json` (`0600`). `jq` queries and event filters run from `/` with module imports and environment access disabled.
- **Private `.bf/` & `logs/` state and secret redaction**: `.bf/` (`write.lock`, `events.jsonl`, `index.jsonl`, `cache/state.json`, `<loop>.json`) and `logs/` (`0600` plain JSON line `*.log` files) are enforced as `0700` owned by `geteuid()`, `bf mcp` redacts host paths and secrets, and diagnostics redact common tokens/keys and strip control characters.
