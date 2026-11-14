# Assist — prompts, messaging, events, queues, and watch

Coordinate agent prompt jobs, inbound/outbound messaging, event subscriptions, `gate:` checks, `bf wake` follow-ups, failure incidents, durable queues, and continuous `bf watch`.

## Prompt jobs (`prompt:` + `harness:`)

Prompt jobs load a Markdown template from `prompts/` (expanded prompt cap: `120,000` bytes), expand placeholders once, and run your external CLI `harness:` (or `defaults.harness`):

```yaml
defaults:
  timezone: Europe/Paris
  post: owner-chat
  harness:
    [
      claude,
      --print,
      --output-format,
      json,
      --tools,
      "Read,Grep,Glob,Bash",
      --allowedTools,
      "Bash(bf read *),Bash(bf search *)",
      --permission-mode,
      dontAsk,
    ]
jobs:
  triage:
    prompt: prompts/triage.md
    extract: /result
    emit: message
    idempotent: true
    retries: 3
    on:
      - { at: "08:00", days: [mon, tue, wed, thu, fri] }
      - { event: { kind: record, source: mail, cooldown: 300 } }
```

- **Prompt inputs**: BF passes the expanded prompt on stdin by default. Put `{{prompt}}` in argv to pass on argv with stdin closed, or `{{context}}` to pass a private JSON file path in `.bf/state/JOB/` (`job`, `run`, `kind`, `start`, `end`, `reason`, `hook`, `args`, `events`, `messages`, `stdin`).
- **Routing (`extract:` + `emit:`)**: `extract: /pointer` reads a JSON string value; `NO_REPLY` or blank output stays quiet (`quiet: true`). `emit: message` delivers through `post` / `defaults.post`; `emit: action` validates `draft` `ACTION.md` output and creates `actions/YYYY-MM-DD_JOB-SUFFIX/ACTION.md`; `emit: log` writes `logs/JOB.log`.
- **Safe permissions & privilege split**: restrict harness behavior via harness flags (`--allowedTools`, `--permission-mode dontAsk`, `--setting-sources user` in shared repos — never `--dangerously-skip-permissions`). Keep scheduled triage read-only (`emit: action` drafts, or `scout`/`actor` split in [`examples/prompts/`](https://github.com/fmind/brain-framework/tree/main/examples/prompts) including the disabled `compile` assistant recipe).

## Channels and messaging (`emit: messages` + `send:`)

- **Inbound (`emit: messages`)**: runs either bounded polls (`{"messages": [...], "cursor": "..."}`) or continuous `on: { daemon: true }` NDJSON line streams; admitted messages persist in `.bf/runtime.sqlite3` and emit `message.received` events (`<= 1,000` messages/batch).
- **Outbound (`send:` + `bf post`)**: `send:` jobs read an outgoing envelope (`id`, `text`, `conversation`, `target`, `reply_to`) on stdin or expand `{{text}}`, `{{target}}`, `{{thread}}` in argv:

```bash
bf post "Launch approved." --to owner-chat --thread t1 --run "$RUN_ID"
```

- **Sender trust boundary**: only attach prompt harnesses to `emit: messages` channels whose adapter authenticates trusted senders (see the channel DM allowlist recipe using `{{lines:...}}` in [`examples/channels/`](https://github.com/fmind/brain-framework/tree/main/examples/channels)); collect multi-sender mail or group rooms via `emit: records`.

## Triggers, canonical events, and `gate:` checks

Each entry in `on:` (up to 32 rules per job) sets one of:

- **Schedule**: `every` (seconds/duration such as `15m`) or `at: "HH:MM"` with optional `days` (`mon`..`sun`) and `timezone` (`defaults.timezone`), never both.
- **Hook**: `hook: EVENT` invoked by `bf run --hook EVENT` (exits `0` cleanly when no enabled job matches).
- **Daemon**: `daemon: true` alone on `emit: messages` jobs under `bf watch`.
- **Event (`on: { event: ... }`)**: reactive subscription with fields:
  - `kind`: one or a list (up to 16) of `"record.added"`, `"record.changed"`, `"record.removed"`, `"note.changed"`, `"message.received"`, `"job.failed"` (`"record"` expands to all three `record.*` kinds).
  - `source`: omitted/`""` (any), one job/source name, or a list of up to 32 names.
  - `match`: optional offline FTS5 search query string (up to 1,024 chars — not regex). Evaluated against `.bf/cache.sqlite3` for `record.added`, `record.changed`, and `note.changed` refs, and against normalized event text (`summary`, `ref`, `source`, `data`) for `record.removed`, `message.received`, and `job.failed`. Non-matches advance the cursor with zero subprocesses.
  - `cooldown` (`0s`–`365d` holds the event cursor to coalesce bursts), `max` (`1`–`10,000` total firings), and `expires` (ISO timestamp cutoff).

Events stay strictly decoupled from record collection/ingest: `bf collect` and `bf ingest` touch `.bf/wakeup`, and `runtime.sync_item_events` diffs `.bf/cache.sqlite3` watermarks inside `bf update` / `bf watch` to emit `record.*` and `note.changed`.

Attach an optional pre-flight `gate:` script (`examples/routines/records-gate.py`) when you need scalar attribute filtering (`--where status=open`): exit `0` proceeds; non-zero skips as `gated` cleanly.

## Wakes, failure incidents, and durable queues

```bash
bf wake --job triage --in 30m --reason "Recheck CI status"
bf wake --at 2026-09-28T08:00:00Z --run "$RUN_ID"
bf queue
bf queue ENTRY_ID --retry
bf queue ENTRY_ID --cancel
```

- **Bounded wakes**: at most `16` active pending wakes total across `.bf/runtime.sqlite3` and recursion depth `<= 8` when chained via `--run`.
- **Failure incidents**: failed runs open or increment a per-job incident row in `.bf/runtime.sqlite3` and emit `job.failed`. When `defaults.post` names a different enabled `send:` job, BF enqueues an opening alert (`[brain] JOB failed: ...`), repeats reminders every `6h` (`21,600s`) while still failing, and sends one `[brain] JOB recovered` message on the next success. Active counts appear under `queue.incidents` in `bf status` and on `bf watch` rows.
- **Explicit recovery**: transient `EX_TEMPFAIL` (`75`) or `idempotent: true` jobs/deliveries retry cleanly up to `retries` (`0`–`20`, capped at 300s backoff). Non-idempotent failures enter `uncertain` / `failed` in `.bf/runtime.sqlite3`: inspect `bf queue ID` (listing hides message bodies) and confirm `--retry` or `--cancel` explicitly. Queue state never blocks collection or retrieval.

## Run continuously (`bf update` and `bf watch`)

```bash
bf update --dry-run
bf update --job mail
bf watch --interval 60 --notify failure --json
bf status --watch
```

- `bf update` runs due record jobs (concurrently), then due scheduled jobs, refreshes the cache, syncs item events, dispatches matching event jobs, runs due wakes, and flushes pending deliveries.
- `bf watch` runs continuous `daemon: true` jobs, reacts immediately to `.bf/wakeup`, and renders an interactive Rich dashboard (`j`/`k` select, `g`/`G` jump, `Tab` attention filter, `f`/`u` reload, Space pause, `s`/`n`/`t`/`i`/`r` sort, `?` help, `q` quit) or JSON Lines (`--json`) for services. A second `bf watch` observes read-only (same as `bf status --watch`). Configure defaults under `watch:` (`interval`, `poll_interval`, `notifications`, `notification_cooldown`).

<details markdown="1">
<summary>Run bf watch --json as a user service (Linux systemd / macOS launchd)</summary>

Set explicit `PATH` including `bf` and provider CLIs, keep credentials in CLI logins or mode-`600` files, and keep service units outside shared Git repos:

Linux (`~/.config/systemd/user/bf-watch.service`, verify with `systemd-analyze --user verify` and `systemctl --user enable --now bf-watch.service`):

```ini
[Unit]
Description=Brain Framework watch

[Service]
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/bf watch --json --brain %h/brain
StandardOutput=null
Restart=on-failure
RestartSec=60
SuccessExitStatus=130

[Install]
WantedBy=default.target
```

macOS (`~/Library/LaunchAgents/bf-watch.plist`, check with `plutil -lint` and `launchctl bootstrap "gui/$(id -u)" ...`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>bf-watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/me/.local/bin/bf</string><string>watch</string><string>--json</string><string>--brain</string><string>/Users/me/brain</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/Users/me/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ExitTimeOut</key><integer>60</integer>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/Users/me/Library/Logs/bf-watch.log</string>
</dict>
</plist>
```

</details>
