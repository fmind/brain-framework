# Watch an offline brain

This fictional example uses only Python and local files. `calendar` changes every 30 seconds, `git` stays unchanged, and `unavailable` intentionally exits 1. No provider, account or network is used. The short refresh intervals are for the demo.

With `bf` installed, retrieve the example from the release matching your installation, copy it into a disposable directory and isolate its local configuration and history:

```bash
watch_demo=$(mktemp -d)
watch_demo=$(cd "$watch_demo" && pwd -P)
git clone --depth 1 --branch "v$(bf --version)" https://github.com/fmind/brain-framework.git "$watch_demo/source"
cp -R "$watch_demo/source/examples/watch" "$watch_demo/brain"
env -u BF_BRAIN XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  bf watch --brain "$watch_demo/brain"
```

From a Brain Framework checkout, copy `examples/watch` instead and run each `bf` command in this guide as `uv run bf`.

`settings/watch.yaml` checks due work every five seconds, reads history every second and keeps notifications off for the intentional failure. Add `--notify all` to try failure/success/recovery alerts; a second `bf watch` observes the active collector without starting another. CLI options override the file.

Inspect the preferences contract without starting a watcher:

```bash
bf schema --kind watch
```

Expect a JSON Schema with `title: Settings`, `interval` defaulting to 60 seconds and a minimum of 5. The example overrides that default to 5. Unknown settings and invalid values fail before any program starts.

Within the first cycle, `calendar` and `git` each show one added record (`1/0/0`); `unavailable` shows `failed`, and manual/disabled sensors do not run. The failed sensor does not rerun every five-second cycle: a failure waits 1, 2, 4… minutes before its retry, never longer than its `refresh`, so this 60-second demo sensor retries once a minute and its Next due column shows that retry time. After 30 seconds the calendar record shows one update (`0/1/0`). Use `j`/`k` or arrows to select a row, Tab to filter attention, `?` for help, Space to pause future checks and `q` to cancel the active update and quit. Nothing is installed in the background. The [scheduling guide](../../docs/docs/schedule.md#watch-collection) shows this dashboard sorted by state.

Try the sorting controls after that first cycle:

| Keys                | Expected visible result                                                                                     |
| ------------------- | ----------------------------------------------------------------------------------------------------------- |
| `i`                 | Panel title ends with `items ↓`; `calendar` and `git` lead with `1` item each. Unknown counts (`—`) follow. |
| `r`                 | Title shows `items ↑`; unknown counts still follow the known counts.                                        |
| `s`                 | Sort becomes state, with `unavailable` first because its last attempt failed.                               |
| `t`                 | Most recent successful update first; programs without a success follow.                                     |
| `n`, then `G` / `g` | Alphabetical order; jump to the last / first row.                                                           |

The selected program stays selected as sorting or new history moves it. Items means records returned in the last successful sensor run, not the total stored catalog. See [dashboard sorting](../../docs/docs/schedule.md#sort-the-dashboard) for every field; view choices last only for the session and also work in the observation dashboard below.

Choose only the successful sensors and generate a one-minute native schedule without activating it:

```bash
env -u BF_BRAIN XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  bf schedule --brain "$watch_demo/brain" \
  --sensor calendar --sensor git --every 1 \
  --output "$watch_demo/brain/settings/schedules"
```

The JSON reply contains the native files and literal argv lists for installation, status and removal. Inspect the files; this example does not require installing them. Their PATH, XDG paths and executable point to the environment used to generate them, so regenerate a real schedule from the installation you intend to keep. One-minute checks can collect the 30-second calendar less frequently than its refresh; the scheduler checks eligibility, it does not override it.

Observe the same local history without executing programs; its footer offers `u reread` instead of `u check` and no pause:

```bash
env -u BF_BRAIN XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  bf status --watch --brain "$watch_demo/brain"
```

After quitting, remove only the disposable directory you created if you no longer need its files:

```bash
rm -rf -- "$watch_demo"
unset watch_demo
```

See [Watch and schedule updates](../../docs/docs/schedule.md) for real installations and platform limits.
