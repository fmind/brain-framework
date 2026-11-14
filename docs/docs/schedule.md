---
icon: lucide/calendar-clock
description: Run or observe due jobs, set watch preferences and keep watch running as a service.
---

# Watch and schedule updates

Run reviewed jobs automatically from one brain:

| Goal                                | Command                                                         |
| ----------------------------------- | --------------------------------------------------------------- |
| Preview or run one update cycle     | `bf update --dry-run`, then `bf update` and `bf status --check` |
| Run continuously in a terminal      | `bf watch`                                                      |
| Observe another collector read-only | `bf status --watch` (runs nothing)                              |
| Run unattended in the background    | `bf watch --json` as a [user service](#run-watch-as-a-service)  |

Set `enabled: false` to disable a job, or omit `on:` for manual-only jobs. Keep CI strictly offline (`bf validate`, `bf eval`).

## Watch collection

```bash
bf watch
```

`bf watch` runs due jobs immediately and rechecks every `interval` seconds (default 60), while continuously running `daemon: true` message streams (`~`), reacting to events, and processing wakes and deliveries (`@` marks message jobs, `*` marks action jobs). Stopping watch terminates all owned process groups cleanly.

[![Watch dashboard for the fictional offline demo: five sensors sorted by state, with a failed sensor's details beside their last success, next due time, item count and record changes.](../assets/watch.svg)](../assets/watch.svg)

| Key                   | Effect                                                                              |
| --------------------- | ----------------------------------------------------------------------------------- |
| `j` / `k`, arrows     | Select a job and inspect its details, log tail and latest action.                   |
| `g` / `G`, Home / End | Jump to first or last job.                                                          |
| `Tab`                 | Filter to jobs needing attention (failed, due, never succeeded once due, degraded). |
| `f` (or `u`)          | Reload `bf.yaml` and check due work now (new jobs still require restarting watch).  |
| Space                 | Pause or resume future checks (`bf watch` only).                                    |
| `?` / `q`             | Toggle key guide / cancel active update and quit (Ctrl-C exits 130).                |

### Sort the dashboard

Press `s` to cycle sort fields, `n` / `t` / `i` to sort by name, last success or returned item count, and `r` to reverse (`bf watch` and `bf status --watch`, minimum 80×24 color terminal, `NO_COLOR` respected).

For service supervision, `bf watch --json` streams JSON Lines snapshots every `poll_interval` seconds (`brain`, `running`, `assisting`, `message`, `jobs` — see [`Row`](https://github.com/fmind/brain-framework/blob/main/src/bf/watch.py)) and writes `bf:` diagnostics to stderr. Only one active collector (`bf watch --json` or primary `bf watch`) may own a brain; a second interactive `bf watch` falls back to observation mode, and concurrent `bf update` runs serialize up to 10 minutes (`another update is still active`).

## Watch preferences

Override defaults on the CLI (`bf watch --interval 300 --notify off`) or persist them in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/schedule/#watch-preferences
watch:
  interval: 300
  notifications: off
```

| Setting                 | Default   | Accepted values                                                                                                     |
| ----------------------- | --------- | ------------------------------------------------------------------------------------------------------------------- |
| `interval`              | `60`      | Seconds between checks for due work (`5`–`86400`).                                                                  |
| `poll_interval`         | `2`       | Seconds between history reads and JSON snapshots (`0.2`–`60`).                                                      |
| `notifications`         | `failure` | `off`, `failure`, `success` or `all` (`osascript`/`notify-send`/`gdbus`, never quoting job names or provider text). |
| `notification_cooldown` | `300`     | Minimum seconds between desktop alerts (`0`–`86400`).                                                               |

## Select jobs {#select-programs}

Filter `bf update` or `bf watch` with repeatable `--job`:

```bash
bf update --job git-commits --dry-run
bf watch --job git-commits --job weekly-review
```

Unknown job names fail up front; `--job` never forces disabled or manual jobs to run.

## Run watch as a service

Run `bf watch --json` under your OS service manager so collection, events and daemons stay up without an open terminal. Set explicit `PATH` (including `bf` and provider CLIs), forward custom `XDG_CONFIG_HOME` or `requires.env` variables, keep [credentials](sensors.md#credentials) in CLI logins or mode-`600` files, and keep service files out of shared Git history.

On Linux, save `~/.config/systemd/user/bf-watch.service`:

```ini
# https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
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

```bash
systemd-analyze --user verify ~/.config/systemd/user/bf-watch.service
systemctl --user daemon-reload
systemctl --user enable --now bf-watch.service
journalctl --user -u bf-watch.service -n 30 --no-pager
```

On macOS, save `~/Library/LaunchAgents/bf-watch.plist` (replacing `/Users/me` with your literal home path):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>bf-watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/me/.local/bin/bf</string>
    <string>watch</string>
    <string>--json</string>
    <string>--brain</string>
    <string>/Users/me/brain</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/me/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <key>ExitTimeOut</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>/dev/null</string>
  <key>StandardErrorPath</key>
  <string>/Users/me/Library/Logs/bf-watch.log</string>
</dict>
</plist>
```

```bash
plutil -lint ~/Library/LaunchAgents/bf-watch.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/bf-watch.plist
launchctl print "gui/$(id -u)/bf-watch"
```

Alternatively, run `bf update --brain BRAIN` from a systemd timer, `StartCalendarInterval` or cron when you need no persistent channel streams. See [systemd lingering](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html), [WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd), [ChromeOS lifecycles](https://www.chromium.org/chromium-os/developer-library/guides/containers/containers-and-vms/#lifecycles) and [Apple LaunchAgents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

## Timing and health

- **Cadence**: `on:` triggers set either `every` seconds or wall-clock `at: "HH:MM"` (`days`, `timezone`), never both (`defaults.timezone` applies when omitted). Keep watch `interval` shorter than your smallest `every`.
- **Gates (`gate:`) and events**: jobs with `gate:` skip unmatched runs as `gated` cleanly; event triggers (`on: { event: ... }`) only fire when matching runtime events arrive.
- **Retries and backoff**: failed scheduled jobs back off `60 * 2**(error_count - 1)` seconds capped by `every` (or 86,400s); `send:` jobs with `idempotent: true` retry deliveries up to 20 attempts (`300s` max delay), while non-idempotent failures wait in `bf queue` as `uncertain`.

Freshness states in `bf status` (local to the collecting machine):

| State     | Meaning                                                                                    |
| --------- | ------------------------------------------------------------------------------------------ |
| `fresh`   | Succeeded within `2 × every` (or within 86,400s of the latest due `at` slot).              |
| `overdue` | Scheduled and previously succeeded, but past its `2 × every` or 86,400s `at` grace window. |
| `never`   | Scheduled with no local current success (joins `attention` once due).                      |
| `manual`  | No `every`, `at` or `daemon` trigger.                                                      |
| `unknown` | Disabled job, `daemon` without error state, or historical source.                          |

`bf status --check` exits 1 whenever [`attention`](commands.md#attention-entries) is non-empty. Every `attention` entry across `bf status`, Home and `bf read review` carries `{"kind": "job", "name": "...", "freshness": "..."}` (plus optional `"failed": true`, `"silent": true` and `"degraded"`).

### Silent sources

Detect broken filters or expired feeds that keep returning 0 changes by setting `silent_after` (seconds above the source's longest quiet spell, such as a weekend):

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
jobs:
  mail:
    run: [sensors/gmail-headers.py, "{{start}}", "{{end}}"]
    emit: records
    on: { every: 3600 }
    silent_after: 172800
```

When an enabled scheduled source has stored records and its newest `changed` timestamp is older than `silent_after`, it marks `"silent": true` in `bf status`, `memories` pages and search [coverage](retrieval.md#source-coverage), joins `attention` and fails `bf status --check` until a new or updated record arrives.
