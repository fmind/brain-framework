# Schedule updates

A timer checks whether your brain needs an update. For example, an hourly sensor (`refresh: 3600`) can be checked every 15 minutes: `bf update` runs it only when it is due. Brain Framework has no background service of its own.

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
bf status --check --brain ~/brain
```

Look for a next trigger in `list-timers`, `Result=success` and `ExecMainStatus=0` in the service result, and a successful BF health check. If the service fails, inspect its journal and `bf status`. Give it a PATH containing `uv`, `bf` and any provider tools its programs need, or use absolute executable paths. An enabled timer alone does not prove successful collection.

To stop future triggers and any active update:

```bash
systemctl --user disable --now bf-update.timer
systemctl --user stop bf-update.service
```

## macOS and CI

On macOS, use a launchd agent with `StartInterval` set to 900 seconds and `ProgramArguments` containing the absolute `bf` path, `update`, `--brain` and the absolute brain path. Give it the PATH its sensors need.

For a shared brain, use one collection job. The [team guide](team.md#collect-in-ci) includes a GitHub Actions example.

## Timing and health

Choose a timer interval shorter than the smallest nonzero `refresh`. With an hourly sensor and a 15-minute timer, a check 59 minutes after the last success skips the sensor; the next check at 74 minutes runs it. An hourly timer could leave it waiting until minute 119.

`Persistent=true` catches up missed calendar triggers when the user manager returns; it does not keep a sleeping laptop awake. Failed programs remain due for the next update.

`bf status --check` exits 1 for an enabled scheduled program that failed, never succeeded locally, or last succeeded more than twice its `refresh` ago. Run this check on the collecting machine: run history is local, even when records are shared.
