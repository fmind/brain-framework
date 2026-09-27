# Watch and schedule updates

Use `bf watch` as the primary way to refresh information and inspect collection. Optionally invoke `bf update` through your operating system's scheduler. Both use the existing `enabled` and `refresh` rules in `bf.yaml`. Watch preferences and native scheduler files stay separate and editable; BF installs no background service.

## Watch collection

```bash
bf watch
```

This starts due sensors and routines immediately and checks again 60 seconds after each completed cycle. It can contact providers. `--interval SECONDS` changes the check interval (minimum 5); each program's `refresh` still decides when it is due. Failed programs retry on the next cycle, so choose an interval compatible with provider limits. Closing the collecting watch stops its collection; it installs nothing. If another watcher owns this physical brain, a second interactive `bf watch` automatically observes it. A competing `watch --json` fails visibly so its native supervisor can retry, rather than remaining an observer without a collector. Its selectors only filter the display; the active collector keeps its own settings. The observer does not take over when the collector exits: restart watch to collect.

The dashboard lists sensor/routine state, last successful collection, next due time and records added, updated and removed in the last successful run. `never` means no local success is recorded. `failed` means the last attempt failed, even if an older success exists. Sensor details also show the last run's elapsed seconds, output bytes and whether it used a wider reconciliation window when those measurements are available. Routines have `*` beside their name and show their latest action in the details pane. Manual and disabled programs are visible but never run automatically. The screen reads small local history files every two seconds; it does not repeatedly index the brain or expose provider text. Use `bf status --check` for the full cache and evidence health check.

| Key               | Effect                                                             |
| ----------------- | ------------------------------------------------------------------ |
| `j` / `k`, arrows | Select a program and inspect its details.                          |
| Tab               | Toggle programs needing attention: failed, never collected or due. |
| `u`               | Check for due work now; respects refresh, selection and pause.     |
| Space             | Pause/resume future updates; an active update finishes.            |
| `?`               | Toggle the guide and explain counts and timing.                    |
| `q`               | Cancel any active update and quit successfully.                    |
| Ctrl-C            | Cancel and exit 130; terminal settings are restored.               |

Use an ordinary color terminal, ideally at least 80 columns by 24 rows. Wide terminals show details beside the table; narrower terminals put them below. Colors follow terminal support and `NO_COLOR`; fonts come from your terminal. Text labels remain meaningful without color.

```bash
bf status --watch   # observe only; never executes programs
bf watch --json     # execute due work and emit JSON Lines
```

Observation is useful alongside a native scheduler. `status --watch` shows program history, not the full `status` report; it cannot be combined with `--check`. `watch --json` writes a snapshot every two seconds for scripts, with `brain`, `observing`, `running`, `message` and `programs`; each program includes its kind, selection, state, refresh, last success, next due time, change counts, elapsed seconds, output bytes, reconciliation flag and safe diagnostic/log path. Unknown counts are null. The active-update flag refers to this watch process. Neither view probes provider health. See the [offline demo](https://github.com/fmind/brain-framework/tree/main/examples/watch) for a runnable success/failure example.

![Fictional offline watch demo: program states and last-run changes beside failure details.](../assets/watch.svg)

## Watch preferences

**No settings file is required.** By default, watch checks due work every 60 seconds, refreshes the display every 2 seconds and alerts on failures and recovery.

For a quieter session:

```bash
bf watch --interval 300 --notify off
```

To keep those preferences, create `settings/watch.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/schedule/#watch-preferences
interval: 300
notifications: "off"
```

| You want to…              | Change…                                                                      |
| ------------------------- | ---------------------------------------------------------------------------- |
| Check due work less often | `interval` (seconds). Each sensor's `refresh` still determines when it runs. |
| Change display speed      | `poll_interval` (seconds); this does not control provider requests.          |
| Choose desktop alerts     | `notifications`: `off`, `failure`, `success` or `all`.                       |
| Space out alerts          | `notification_cooldown` (seconds).                                           |

CLI options override the file; omitted settings use defaults. Restart watch after editing preferences. Sensor `enabled` and `refresh` belong in `bf.yaml` and reload each cycle.

<details markdown="1">
<summary>Defaults, validation and notification behavior</summary>

| Setting                 | Default   | Accepted values                                            |
| ----------------------- | --------- | ---------------------------------------------------------- |
| `interval`              | 60        | Integer 5–86400 seconds.                                   |
| `poll_interval`         | 2         | Number 0.2–60 seconds.                                     |
| `notifications`         | `failure` | `off`, `failure`, `success`, `all`; quote `"off"` in YAML. |
| `notification_cooldown` | 300       | Integer 0–86400 seconds.                                   |

- Unknown keys or invalid values fail before execution, even if overridden on the CLI.
- `failure` alerts on new or changed failures and recovery. `success` alerts on completed cycles with work and recovery. `all` includes both.
- Identical failures and idle cycles stay quiet. Cooldown may coalesce transient states; notification state resets on restart.
- Alerts contain generic status, never source names, paths or provider text. `bf status --watch` sends no alerts.
- Desktop notifications need `osascript` on macOS or a Linux notification service with `notify-send` or `gdbus`. ChromeOS/WSL need a working bridge. Delivery failure warns once without stopping collection; desktop settings may suppress display.

</details>

## Select programs

Repeat `--sensor` and `--routine` on `update`, `watch` or `schedule`:

```bash
bf update --sensor git-commits --dry-run
bf watch --sensor git-commits --routine weekly-review
bf schedule --sensor git-commits --name git-only
```

Substitute names from your `bf.yaml`. With no selectors, all configured programs are eligible, including programs added later. With any selector, **only named programs** are eligible; for example `--sensor git-commits` excludes every routine and every other sensor. Unknown names fail before execution; disabled programs and `refresh: 0` stay excluded. Selectors never force a run or modify configuration. Referenced brains never execute.

## Generate a native schedule

```bash
bf schedule --every 15 --output ~/brain/settings/schedules
```

The result contains `files`, `written` paths, the command `argv`, and literal argv lists named `install`, `status` and `remove`. It writes native files; it does not run those commands. Without `--output`, it only previews the result as JSON. Existing identical files are safe to regenerate; differing files are preserved and generation fails, so review your edits or generate to a fresh directory. An output directory can live outside the brain too.

| Option/default         | Meaning                                                                                                                                          |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--backend auto`       | launchd on macOS; systemd on Linux when `systemctl` is on PATH; otherwise cron. Detection does not prove the scheduler is running.               |
| `--every 15`           | Check every 15 minutes. Supported minute intervals divide an hour: 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60.                                     |
| `--name update`        | Job suffix; use different names for different selections. Brain identity and path also distinguish generated jobs.                               |
| `--executable PATH`    | Absolute installed `bf`; default is beside the running Python. Regenerate if that installation moves.                                            |
| PATH and XDG locations | Capture absolute PATH entries and explicitly set `XDG_CONFIG_HOME`/`XDG_STATE_HOME`. Credentials and other environment variables are not copied. |

Generation rejects control characters in paths/environment values, quotes or backslashes in a systemd executable path, and combined backslashes/percent signs that cron cannot safely represent. Use a simpler installation path or another backend in those cases.

Review the programs and generated environment, then follow the returned installation commands when you want recurring execution. Configure provider authentication separately. Those argv arrays are data: quote arguments if translating them to shell commands. For cron, copy the single generated job line into `crontab -e`; **do not run `crontab FILE`**, which replaces existing jobs. For systemd, verify with `systemd-analyze --user verify FILE.service FILE.timer`; for launchd use `plutil -lint FILE.plist` before installation.

Systemd definitions use calendar triggers, missed-trigger catch-up, up to 30 seconds of jitter and a 45-minute total update limit. LaunchAgents use calendar minute triggers so sleep-time occurrences coalesce on wake; missed power-off runs are not replayed. Cron skips missed occurrences. Every backend retains per-program BF timeouts; launchd/cron have no additional total-job timeout in these files. An enabled schedule does not prove collection success: inspect native job results and `bf status --check` on the collecting machine.

On Linux use a working systemd user manager when available; macOS uses a logged-in user's LaunchAgent. ChromeOS runs BF inside its Linux environment, whose processes stop at logout. WSL needs a running distribution; systemd services do not keep it alive. Generation supports these native formats, but cannot guarantee an always-running host. See the [ChromeOS lifecycle documentation](https://www.chromium.org/chromium-os/developer-library/guides/containers/containers-and-vms/#lifecycles), [WSL systemd documentation](https://learn.microsoft.com/en-us/windows/wsl/systemd) and [Apple scheduling guidance](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html).

Use one execution owner per selected program. Concurrent `update` cycles on the same brain fail visibly instead of duplicating a due list; manual `collect` retains its own per-program lock. Use `bf status --watch` when a timer owns collection; timer ownership cannot be detected by the watch lock. Removing a schedule requires both stopping future triggers and accounting for an active update; the returned removal commands cover both for systemd/launchd. Removing a cron line stops future triggers only.

Keep arbitrary backup scripts or other non-BF jobs in their own native files beside these definitions. Edit those files directly using the native scheduler's rules; BF does not add a generic task schema or execute arbitrary schedule commands.

## Check before scheduling

From `~/brain`, check the programs before installing a timer:

```bash
bf update --dry-run    # list due work; execute nothing
bf update              # run due sensors, then routines
bf status --check
```

The preview lists due sensors and routines without executing them. The next command runs them; a healthy `bf status --check` exits 0. Review the configured programs before that run. Set `enabled: false` to stop one program, or `refresh: 0` to keep a sensor manual. Updates act on selected roots, never their referenced brains.

## Linux

Create these files under `~/.config/systemd/user/`. Replace the executable and brain paths with your own; `command -v bf` shows the executable path.

```ini
# https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
# bf-update.service
[Unit]
Description=Update Brain Framework

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/bf update --brain %h/brain
TimeoutStartSec=45min
```

```ini
# https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html
# bf-update.timer
[Unit]
Description=Check for due brain updates every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=1min

[Install]
WantedBy=timers.target
```

Enable the timer and start one update now so you can check its result:

```bash
systemctl --user daemon-reload
systemctl --user enable --now bf-update.timer
systemctl --user start bf-update.service
systemctl --user list-timers bf-update.timer
systemctl --user show bf-update.service -p Result -p ExecMainStatus
journalctl --user -u bf-update
bf status --check
```

Look for a next trigger in `list-timers`, `Result=success` and `ExecMainStatus=0` in the service result, and a successful BF health check. If the service fails, inspect its journal and `bf status`. Give it a PATH containing `uv`, `bf` and any provider tools its programs need, or use absolute executable paths. An enabled timer alone does not prove successful collection.

To stop future triggers and any active update:

```bash
systemctl --user disable --now bf-update.timer
systemctl --user stop bf-update.service
```

## macOS and CI

On macOS, use a launchd agent with `StartInterval` set to 900 seconds and `ProgramArguments` containing the absolute `bf` path, `update`, `--brain` and the absolute brain path. Give it the PATH its sensors need.

For a shared source, designate one collecting laptop. See [team collection](team.md#collect-on-a-laptop).

## Timing and health

Choose a timer interval shorter than the smallest nonzero `refresh`. With an hourly sensor and a 15-minute timer, a check 59 minutes after the last success skips the sensor; the next check at 74 minutes runs it. An hourly timer could leave it waiting until minute 119.

`Persistent=true` catches up missed calendar triggers when the user manager returns; it does not keep a sleeping laptop awake. Failed programs remain due for the next update.

`bf status --check` exits 1 for an enabled scheduled program that failed, never succeeded locally, or last succeeded more than twice its `refresh` ago. Run this check on the collecting machine: run history is local, even when records are shared.
