# Collection recovery and ongoing refresh

Use for failed or overdue collection, backfills, interrupted writes, watch and schedules, upgrades or brain instructions. Run commands inside the selected brain or with `--brain PATH`; unattended jobs always name the path. Live commands run configured code with the user's permissions and need authority covering the selected providers and scope.

## Diagnose and recover

| Symptom                   | Next check                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Failed sensor or routine  | `bf status` shows `failed`, `error`, consecutive `failures` and its `log`, `logs/NAME.log` in the brain. Read the log locally; within live authority, reproduce with `bf collect SENSOR --since 1d --dry-run` or `bf run --dry-run ROUTINE`. Fix it with a fake-provider regression before retrying.       |
| Overdue source            | Freshness is `overdue` when a scheduled program is late by more than twice its `refresh`. Check the watcher or native scheduler and its logs; `bf status --watch` observes without running anything. Automatic catch-up covers at most 30 days: an older gap needs `bf collect SENSOR --since YYYY-MM-DD`. |
| Snapshot removal rejected | `snapshot would remove N of M records`: an empty reply for a non-empty catalog, or more than half and more than 10 records removed. Check account, scope and mounts with `bf collect SENSOR --dry-run`; for an intended removal, run `bf collect SENSOR --allow-removal` once. Never delete `memories/`.   |
| Busy or stale cache       | `cache` is `busy` while another writer works; replies then carry `stale` and serve the last generation. Retry after the writer finishes; a watcher or update in progress is the usual writer.                                                                                                              |
| Interrupted write         | `pending_transaction: true`: an interrupted record commit waits in `memories/.pending/`. Preserve it, then run `bf build` (or let the next `bf update`) recover it before the cache refreshes.                                                                                                             |
| Skipped files             | `problems` name the file (`bf validate` explains it); `bf build` and `bf update` report a `skipped` count and exit 1 until it is fixed. A damaged cache is discarded and rebuilt by itself.                                                                                                                |
| Retrieval miss            | Add the question to `evals/` before changing the owning note or the sensor's mappings; compare `bf eval` replies with `--baseline` so a lower `rank` shows. Never weaken assertions.                                                                                                                       |
| Scan-limit warning        | `warnings` name a source or folder above 80% of its 100,000-entry scan limit. Split the sensor into several sources or archive older records outside the brain before scans fail.                                                                                                                          |

Inspect each source's `state` (`active`, `disabled`, or `historical` for records kept after a sensor left `bf.yaml`), `last_collected`, `window` and `last_run` before interpreting freshness; `bf read memories` and `bf read memories/SOURCE` show coverage and the latest records. Only `.bf/` is disposable: `bf build` recreates it. Preserve records, logs, action files and pending journals. Review a shared brain's `bf.yaml`, sensors and routines before running them; registering a brain only makes it searchable. Provider authentication belongs to the provider's own tool: an available executable does not prove access to the right account.

## Watch or schedule

`bf watch` is the primary refresh mode: it runs due sensors, then due routines, in an interactive dashboard until the user quits. Repeat `--sensor NAME` or `--routine NAME` to restrict it; with any selector, only the named programs run. Preferences (check interval, notifications) live under `watch:` in `bf.yaml` and command options override them; restart the watcher after editing them. Agents observe with `bf status`, or stream `bf watch --json` rows when authorized to collect. Keep one execution owner per program in each brain: for a shared source, one collecting machine, so clones do not collect it twice. See [watch and schedule](https://fmind.github.io/brain-framework/docs/schedule/).

With recurring-execution authority, `bf schedule --sensor SENSOR --output settings/schedules` writes native systemd, launchd or cron files and returns the activation, status and removal commands; without `--output` it previews them, and it never activates anything. The files capture this machine's `PATH`, home and brain paths, so keep them out of a shared brain's Git. Check more often than the shortest `refresh`, and regenerate schedules after an upgrade moves the `bf` executable. Report generated, enabled and observed runs separately.

## Windows and freshness

For mutable window sources, use a short `refresh` and an `overlap`, plus `reconcile: {refresh: 86400, lookback: 604800}` when a daily seven-day revisit is justified. Preview the planned windows with `bf update --dry-run`. Compare `last_run` counts, `elapsed_seconds` and `output_bytes`; output bytes do not measure provider traffic. A failed program retries after 1 minute, doubling per consecutive failure up to its `refresh`; `bf collect SENSOR` retries a sensor at once. A backfill ending before the recorded coverage leaves `last_collected` unchanged, and a manual sensor's latest run sets its window, so backfill gaps explicitly ([backfills and coverage](https://fmind.github.io/brain-framework/docs/sensors/)). A fresh cache, a successful run or a current dashboard never proves complete provider truth.

## Upgrade Brain Framework

Stop watchers and schedules, read the release's changelog and back up the idle brain. After `uv tool upgrade brain-framework` (or updating a brain's pinned `uv.lock`), follow the changelog's manual steps, then:

```bash
bf --version
bf validate
bf eval
bf skills ~/.agents/skills --check
```

`bf skills DIR --check` reports each skill as `current`, `outdated`, `modified`, `unmanaged` or `missing`; `bf skills DIR` updates the unedited ones and leaves edited folders alone unless the user accepts `--force`. Regenerate native schedules with `bf schedule` when asked by the changelog, then restart watchers and agent hosts.

## Refresh brain instructions

`bf init` writes a brain's `AGENTS.md` once. After an upgrade, compare it with the current template:

```bash
scratch="$(mktemp -d)"
bf init "$scratch/brain" --name NAME
diff "$scratch/brain/AGENTS.md" PATH/AGENTS.md
rm -r "$scratch"
```

No output means they match. Otherwise merge the new guidance by hand, keep the owner's additions, run `bf validate` and show the diff.

## Selected GitHub history

Use the reviewed [GitHub history walkthrough](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md) for a year of commits reachable from `main` and open and closed issues and pull requests. Confirm the repository and branch, and use one window source per repository. Backfill adjacent bounded windows through the present, recording fixed endpoints and successful counts in a private checklist; resume the first unfinished interval and split failed ones without skipping them. Keep incremental refresh plus periodic reconciliation afterwards, with one execution owner per source. A new machine has new local run state: synced records alone do not establish its backfill coverage.
