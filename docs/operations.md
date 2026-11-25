---
icon: lucide/shield-check
description: Compose explicit, least-privilege loops with per-loop env_from allowlists, per-trigger windows, stdout/stderr JSONL streams, when triggers, and headless bf watch.
---

# Operations

Run deterministic sensors, maintenance routines, coding agents, and stream listeners under one explicit `loops:` contract in `bf.yaml`. Supervise everything headlessly with `bf watch`, inspect health with `bf status`, and manage state explicitly with `bf pause`, `bf resume`, and `bf state`.

## Start with one loop {#ingest-sensor-records}

Every loop is either a **cycle loop** (triggered on a schedule, event, or manual `bf run`) or a **daemon** (`when: daemon`, kept running continuously by `bf watch`). Start with one sensor loop in `bf.yaml`:

```yaml
# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json
version: 19
name: my-brain
loops:
  github-issues:
    description: Collect GitHub issues incrementally into memories/github-issues.jsonl.
    run: [python3, sensors/github-issues.py, owner/repo]
    env_from: [GH_TOKEN]
    ingest: upsert
    when: 30m
```

Test, run, and supervise it from the CLI:

```bash
# Preview the resolved argv, environment, and window without executing
bf run github-issues --dry-run

# Execute once now and inspect health
bf run github-issues
bf status --format text

# Run all scheduled, event-driven, and daemon loops continuously
bf watch

# Monitor live from another terminal
watch -n 2 bf status --format text
```

Starter stdlib sensors live in [examples/sensors/](https://github.com/fmind/brain-framework/tree/main/examples/sensors) (`git-history.py`, `github-issues.py`, `gmail-headers.py`, `local-documents.py`, and self-monitoring `agent-telemetry.py`).

## Complete multi-loop `bf.yaml`

Combine concurrency pools, windowed sensors, event-driven review, continuous daemons, and agent triage in one file:

```yaml
# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json
version: 19
name: my-brain
description: Personal knowledge base and operational sensors.
timezone: UTC
concurrency:
  default: 4
  agents: 2
  interactive: 2
search:
  exclude: [rss-feeds]
loops:
  github-issues:
    description: Collect GitHub issues incrementally into memories/github-issues.jsonl.
    run: [python3, sensors/github-issues.py, owner/repo]
    env_from: [GH_TOKEN]
    ingest: upsert
    window:
      backfill: 7d
      overlap: 5m
    when:
      - 30m
      - every: 1d
        overlap: 1d
  agent-telemetry:
    description: Collect harness token/prompt usage into memories/agent-telemetry.jsonl.
    when: 1h
    run: [python3, sensors/agent-telemetry.py, claude, ~/.claude/projects, "{{start}}", "{{end}}"]
    ingest: upsert
    # After creating schemas/usages.json, enforce a shared domain schema with:
    # ingest: { source: agent-telemetry, schema: usages }
  review:
    description: Check for notes made stale by newly ingested records.
    when:
      event: records.ingested
      debounce: 10s
    run: [bf, review]
  chat:
    description: Stream incoming chat messages as loop.message events.
    when: daemon
    run: [python3, routines/local.py, listen]
    emit: loop.message
  triage:
    description: Triage incoming messages or self-scheduled wakeups (agents/triage.sh).
    pool: interactive
    timeout: 15m
    env_from: [ANTHROPIC_API_KEY]
    window:
      backfill: 1w
    when:
      - "30 7 * * 1"
      - event:
          kind: loop.message
          source: chat
          where:
            data.direct: true
      - event:
          kind: loop.wake
          where:
            data.target: triage
    emit: loop.event
```

## Loop mechanics

### Folder conventions and concurrency pools

- **Explicit paths, zero hidden defaults**: omit `run:` when a zero-argument script lives at `<dir>/<name>`, `<dir>/<name>.sh`, or `<dir>/<name>.py` under `sensors/`, `routines/`, or `agents/` (ambiguous duplicates fail closed; scripts taking arguments declare `run:`). Always declare `ingest:` and `timeout:` (default `5m`) explicitly on cycle loops.
- **Named pools and priority**: set `concurrency:` to an integer (`1..32`, default `4`) or a pool map (`{default: 4, agents: 2, interactive: 2}`), and assign cycle loops with `pool: <name>` so long agents never starve fast sensors or chat replies. Event-triggered loops always dispatch ahead of schedules.
- **Supporting folders**: keep ad-hoc API scripts in `clients/` and reusable LLM prompts in `prompts/`.

### Per-loop `env_from:` and `.env` isolation

- **Minimal base environment**: loops inherit only basic shell, locale (`LANG`/`LC_ALL` pinned to `C.UTF-8`), TLS CA/proxy vars, and `XDG_{CONFIG,DATA,CACHE,STATE}_HOME`.
- **Explicit secret opt-in**: host credentials (`GH_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`, `SSH_AUTH_SOCK`) and `0600` `.env` secrets are forwarded only when listed in that loop's own `env_from:`.
- **Blocked keys**: loader, interpreter, TLS, proxy, and git override keys (`LD_*`, `DYLD_*`, `PYTHON*`, `GIT_*`, `XDG_*`, `NPM_CONFIG_*`, `OPENSSL_*`, `SSL_CERT_*`, `SSLKEYLOGFILE`, `CURL_HOME`, `*_PROXY`, `BASH_ENV`, `BASHOPTS`, `SHELL`, `NODE_OPTIONS`, `PATH`, `HOME`, `BF_*`) are rejected in `bf.yaml` `env:`, `env_from:`, and `.env`.
- **Precedence**: minimal host allowlist < `bf.yaml` `env:` < `loops.<name>.env:` < `loops.<name>.env_from` (host < `0600` `.env`) < protected `BF_*`.

### UNIX two-stream contract (`stdin`, `stdout`, `stderr`)

- **`env`, `{{...}}` placeholders, and `stdin`**: loops receive `BF_*` env vars, and `run:` expands 7 allowlisted `{{...}}` placeholders (see [Reference](reference.md#loop-environment-placeholders-and-event-predicates)). Event-triggered loops receive only matched JSONL events on `stdin` (empty for scheduled/manual runs; `/dev/null` for daemons).
- **`stdout` (data plane)**: JSONL records for `ingest:` or JSONL events for `emit:`.
- **`stderr` (observability plane)**: structured JSONL or plain text, automatically redacted against forwarded secrets and common credential patterns, then appended live under `.bf/logs.lock` to `logs/<loop>.jsonl`.

### Triggers, windows, `debounce:`, and `where:`

| Trigger shape                                            | Behavior                                                                                 |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Omit `when:`                                             | Manual only (`bf run <loop>`)                                                            |
| `when: daemon`                                           | Continuous process supervised by `bf watch`                                              |
| `30m`, `@daily`, `"30 7 * * 1"`                          | Periodic interval or 5-field cron schedule                                               |
| `{every: 1d, backfill?: 7d, overlap?: 1h}`               | Schedule with per-trigger window overrides (`cron:` supported too)                       |
| `{event: "records.ingested", debounce?: 10s}`            | Event match with optional trailing-edge coalescing (`1s..1h`, max wait `4x`)             |
| `{event: {kind: ..., source?: ..., where?: {...}}, ...}` | Pure-Python dot-path predicate (exact scalar, `{in}`, `{exists}`, `{prefix}`; zero `jq`) |

- **Durations and windows**: every duration requires a unit (`30s`, `5m`, `1h`, `7d`, `1w`; `0`/`0s` is valid only on `overlap:`). When multiple schedules fire together, `BF_START` spans the widest window.
- **Independent backoff and loop guards**: schedule failure backoff never blocks event triggers (events retry up to 3 times on a short backoff before dead-lettering, incrementing `dead_lettered`). Self-sourced `loop.succeeded`/`loop.failed` events are suppressed unless `source:` names the loop, and `bf watch` rate-limits runaway event loops (`10`/min).

### State, timers, live pause, supervision, and storage

- **Event bus and cursors**: `emit: <kind>`, `bf emit`, `bf build`, `bf ingest`, and triggered loops append deduplicated events to `.bf/state/events.jsonl` (`0600`, compacted at `10,000` lines / `16 MiB`). New or reset loops anchor at current `event_seq` so history never replays; lagging loops surface `events_missed` in `bf status`.
- **Delayed wakeups and inspection**: schedule durable timers with `bf emit KIND --in DURATION | --at ISO` into `.bf/state/timers.jsonl` (promoted by `bf watch` and `bf run --due`), and inspect events or timers read-only via `bf events [--timers]`.
- **Live pause and locks**: mute or unmute cycle loops and daemons without editing `bf.yaml` via `bf pause [LOOP ...] [--for DURATION]` and `bf resume`. Per-loop `.bf/locks/loop-<name>.lock` files prevent concurrent overlap and flag crashed loops in `bf status`.
- **Headless `bf watch`**: holds `.bf/watch.lock`, polls managed folders via stdlib `O_NOFOLLOW` scanning (skipping `.git/`, `.venv/`, `memories/`, `logs/`), promotes due timers, honors live pause/resume, and supervises builds, daemons, and pooled cycle loops under `systemd --user` or `journald`. `--format auto` picks `text` on TTY and `jsonl` when piped, and `SIGTERM` exits `143` after draining daemons.
- **Atomic JSONL storage and guards**: `bf ingest` writes `memories/<source>.jsonl` atomically (deleting empty files for DuckDB) and updates `.bf/cache/index.sqlite3` best-effort. It honors `memories/<source>.forgotten`, holds watermarks when `incomplete` is set (`degraded` in `bf status`), and blocks emptying a source or dropping >50% of 10+ records without `--allow-removal`.

## Maintenance runbook

**Reset a stuck loop cursor**

1. Check `bf status --loops` and back up `.bf/state/<loop>.json` if you want a rollback point.
2. Preview with `bf state reset <loop> --dry-run`, then run `bf state reset <loop>`.
3. Blast radius: clears failure counters, schedule anchor (next run re-backfills `window.backfill`), and active `paused` state while anchoring `event_seq` at the latest event. Verify with `bf status --format text`.

**Prune removed loop artifacts**

1. Preview with `bf state prune --dry-run` and copy any `.bf/state/<loop>/` or `logs/<loop>.jsonl` files you want to keep.
2. Run `bf state prune`.
3. Blast radius: permanently deletes orphaned state files, state dirs, unlocked lock files, and logs for loops removed from `bf.yaml` (active locked loops are skipped). Verify `orphans` clears in `bf status --format text`.

**Rebuild a corrupt or stale FTS5 cache**

1. Remove only `.bf/cache/` (`rm -rf .bf/cache`, never `.bf/state/`).
2. Run `bf build` and confirm `index_stale` disappears in `bf status --format text`.

## Limits and safeguards {#limits-and-safeguards}

- **Confined `O_NOFOLLOW` access**: `bf` opens every brain file through `O_NOFOLLOW` descriptors, rejects symlinks inside managed folders, and never traverses sibling files inside `index.md` bundles.

| Resource                          | Limit                                                                                     |
| --------------------------------- | ----------------------------------------------------------------------------------------- |
| `bf.yaml` / `schemas/*.json`      | `1 MiB` (`.env`: `64 KiB`)                                                                |
| Markdown note / `bf ingest` batch | `4 MiB` note / `16 MiB` batch                                                             |
| Single JSONL file                 | `256 MiB` hard cap (`<= 20k` records sweet spot; partition by year with `ingest.schema:`) |
| Tree scan                         | `100,000` entries per source, `1,000,000` total records, `64` directory levels            |

## Privacy and security boundaries {#privacy-and-security-boundaries}

- **No built-in LLM or provider SDK**: `bf` never calls model APIs directly. Loop commands run via literal `argv` (`shell=False`) in isolated process groups, using `python -P -E -m bf` for internal subcommands.
- **Private runtime directories**: `.bf/` (`0700`) separates disposable `.bf/cache/index.sqlite3`, `.bf/state/`, and runtime locks (see [Reference](reference.md#brain-layout) for exact files). `logs/` (`0700`, `0600` files) stays excluded from `bf read`, `bf query`, and note links, and all private paths enforce `geteuid()` ownership.
