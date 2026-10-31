# Watch an offline brain

This fictional example uses only Python and local files. `calendar` changes every 30 seconds, `git` stays unchanged, and `unavailable` intentionally exits 1. No provider, account or network is used. The short refresh intervals are for the demo.

With `bf` installed, retrieve the example from the release matching your installation and copy it into a disposable directory:

```bash
watch_demo=$(mktemp -d)
watch_demo=$(cd "$watch_demo" && pwd -P)
git clone --depth 1 --branch "v$(bf --version)" https://github.com/fmind/brain-framework.git "$watch_demo/source"
cp -R "$watch_demo/source/examples/watch" "$watch_demo/brain"
echo "watch_demo=$watch_demo"
```

From a Brain Framework checkout, copy `examples/watch` instead and run each `bf` command in this guide as `uv run --project CHECKOUT bf`, where `CHECKOUT` is the checkout's path, so it also works after you change into the brain.

Copy the printed `watch_demo=…` line, then start the watcher with its own local configuration and history; its full-screen dashboard covers this terminal until you press `q`:

```bash
env -u BF_BRAIN XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  bf watch --brain "$watch_demo/brain"
```

Leave it running and run the other commands in this guide in a second terminal: paste the copied `watch_demo=…` line there first.

The `watch` section in `bf.yaml` checks due work every five seconds, reads history every second and keeps notifications off for the intentional failure. Add `--notify all` to try failure/success/recovery alerts; a second `bf watch` with the same `XDG_STATE_HOME` observes the active collector without starting another. CLI options override `watch` values; restart the watcher after changing these preferences.

Inspect the preferences contract without starting a watcher:

```bash
bf schema
```

Expect a JSON Schema with `title: Config` and a `watch` property referencing `WatchSettings`, whose `interval` defaults to 60 seconds with a minimum of 5. The example overrides that default to 5. Unknown settings and invalid values fail before any program starts.

Within the first cycle, `calendar` and `git` each show one added record (`1/0/0`); `unavailable` shows `failed`, and manual/disabled sensors do not run. The failed sensor does not rerun every five-second cycle: a failure waits 1, 2, 4… minutes before its retry, never longer than its `refresh`, so this 60-second demo sensor retries once a minute and its Next due column shows that retry time. After 30 seconds the calendar record shows one update (`0/1/0`). Use `j`/`k` or arrows to select a row, Tab to filter attention, `?` for help, Space to pause future checks and `q` to cancel the active update and quit. Nothing is installed in the background. The [scheduling guide](../../docs/docs/schedule.md#watch-collection) shows this dashboard sorted by state.

Try refreshing after adding a source. In the second terminal, run `cd "$watch_demo/brain"`, then:

```bash
python3 - <<'PYTHON'
from pathlib import Path

path = Path("bf.yaml")
text = path.read_text()
if "  new-calendar:" not in text:
    path.write_text(text + "  new-calendar:\n    command: [python3, sensors/demo.py, calendar]\n    refresh: 3600\n")
PYTHON
```

Press `f` in the watcher (`u` also works). Expect `new-calendar` to appear and collect one record without restarting. To make the shortcut's timing visible, start this demo with `--interval 300`; otherwise its five-second checks may collect the new source before you press the key. The header then shows `interval too slow, press ?`, since the 30-second `calendar` runs late between checks. Refresh during an active update queues one follow-up check; repeated presses coalesce. While paused, the row still appears but collection waits for Space. Existing fresh programs, failures waiting for retry, disabled programs and manual programs keep their normal timing. An observer only rereads configuration and history. This adds a fictional local sensor, not a live provider.

Try the sorting controls after that first cycle:

| Keys                | Expected visible result                                                                                     |
| ------------------- | ----------------------------------------------------------------------------------------------------------- |
| `i`                 | Panel title ends with `items ↓`; `calendar` and `git` lead with `1` item each. Unknown counts (`—`) follow. |
| `r`                 | Title shows `items ↑`; unknown counts still follow the known counts.                                        |
| `s`                 | Sort becomes state, with `unavailable` first because its last attempt failed.                               |
| `t`                 | Most recent successful update first; programs without a success follow.                                     |
| `n`, then `G` / `g` | Alphabetical order; jump to the last / first row.                                                           |

The selected program stays selected as sorting or new history moves it. Items counts the records the last committed collection returned, even a backfill, not the stored total. Press `?` for every sort field, or see [dashboard sorting](../../docs/docs/schedule.md#sort-the-dashboard); view choices last only for the session and also work in the observation dashboard below.

Observe the same local history without executing programs; its footer offers `u reread` instead of `f refresh` and no pause:

```bash
env -u BF_BRAIN XDG_CONFIG_HOME="$watch_demo/config" XDG_STATE_HOME="$watch_demo/state" \
  bf status --watch --brain "$watch_demo/brain"
```

After quitting the watcher and the observer, remove only the disposable directory you created if you no longer need its files:

```bash
rm -rf -- "$watch_demo"
unset watch_demo
```

See [Watch and schedule updates](../../docs/docs/schedule.md#run-watch-as-a-service) to keep watch running as a service, and its platform limits.
