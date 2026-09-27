# Watch an offline brain

This fictional example uses only Python and local files. `calendar` changes every 30 seconds, `git` stays unchanged, and `unavailable` intentionally exits 1. No provider, account or network is used. The short refresh intervals are for the demo.

From the Brain Framework checkout, copy the example into a disposable directory and isolate its local configuration and history:

```bash
watch_demo=$(mktemp -d)
cp -R examples/watch "$watch_demo/brain"
XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  uv run bf watch --brain "$watch_demo/brain"
```

`settings/watch.yaml` checks due work every five seconds, reads history every second and keeps notifications off for the intentional failure. Add `--notify all` to try failure/success/recovery alerts; a second `bf watch` observes the active collector without starting another. CLI options override the file.

Within the first cycle, `calendar` and `git` each show one added record; `unavailable` shows `failed`, and manual/disabled sensors do not run. After 30 seconds the calendar record shows one update. Use `j`/`k` or arrows to select a row, Tab to filter attention, `?` for help, Space to pause future checks and `q` to cancel the active update and quit. Nothing is installed in the background.

Choose only the successful sensors and generate a one-minute native schedule without activating it:

```bash
XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  uv run bf schedule --brain "$watch_demo/brain" \
  --sensor calendar --sensor git --every 1 \
  --output "$watch_demo/brain/settings/schedules"
```

The JSON reply contains the native files and literal argv lists for installation, status and removal. Inspect the files; this example does not require installing them. Their PATH, XDG paths and executable point to the environment used to generate them, so regenerate a real schedule from the installation you intend to keep. One-minute checks can collect the 30-second calendar less frequently than its refresh; the scheduler checks eligibility, it does not override it.

Observe the same local history without executing programs:

```bash
XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  uv run bf status --watch --brain "$watch_demo/brain"
```

After quitting, remove only the disposable directory you created if you no longer need its files. See [Watch and schedule updates](../../docs/docs/schedule.md) for real installations and platform limits.
