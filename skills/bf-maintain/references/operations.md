# Collection recovery and ongoing refresh

Use for failed or stale collection, backfills, watch configuration, native schedules or brain instructions. Run commands inside the selected brain, or pass `--brain PATH`; unattended jobs use an explicit path. `update`, `collect`, `watch` and `schedule` act on that one brain, never its references or other registered brains. Live commands run configured code with the user's permissions and require authority covering the selected providers and scope.

## Diagnose and recover

| Symptom                   | Next check                                                                                                                                                                                                                                                                                                                        |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Failing sensor            | `bf status` shows `failed`, `error`, consecutive `failures` and the bounded private `log`. Read the log locally; reproduce with `bf collect SENSOR --since 1d --dry-run` only within live-run authority. Keep provider text out of reports. Fix with a fake-provider regression before retrying.                                  |
| Stale sensor              | Inspect the active watcher or native scheduler and its logs. `bf status --watch` observes without collecting. Automatic catch-up is bounded to 30 days; an older backfill needs an explicit `--since YYYY-MM-DD` and matching scope.                                                                                              |
| Snapshot removal rejected | `snapshot would remove N of M records`: nothing returned for a non-empty catalog, or more than half and more than 10 removed. Check account, scope and mounts with `bf collect SENSOR --dry-run`; for an intended, authorized removal, run `bf collect SENSOR --allow-removal` once. Never delete `memories/SENSOR/` to force it. |
| Duplicate record IDs      | Run `bf validate`, preserve competing revisions and use the [conflict guide](conflicts.md). Renaming a duplicate does not create valid independent evidence.                                                                                                                                                                      |
| Incomplete retrieval      | Inspect `problems` (`error`, with `brain` and `file` when known) and `stale`, repair the named file or brain, then repeat the request. `bf eval` rejects incomplete answers.                                                                                                                                                      |
| Retrieval miss            | Add the question to `evals/` before improving the owning note or sensor projection; compare `bf eval` replies with `--baseline` so a lower `rank` shows. Do not weaken assertions or tune the core for one query.                                                                                                                 |
| Scan-limit warning        | `bf status` `warnings` name a source or authored folder above 80% of its 100,000-entry scan limit, without failing `--check`. Split the source's sensor into several sources or archive older records outside the brain before scans fail.                                                                                        |

Inspect each source's `state` (`active`, `disabled` or `historical`), `last_collected`, `window` and `last_run` counts before interpreting freshness; `bf read memories` and `bf read memories/SOURCE` show each source's coverage and paginated latest records. Preserve `memories/.pending/` during recovery and backup; it holds durable originals for interrupted writes. Inspect the brain, then run `bf build` for explicit recovery; `bf update` and watch cycles also recover before refreshing the cache. Both report a `skipped` count and exit 1 when files were skipped, which `bf validate` names. Search and read do not apply pending journals.

Only `.bf/` is disposable; `bf build` recreates it. Preserve records, action inputs/outputs and pending journals. Review a shared brain's `bf.yaml`, sensors and routines before running them; registering it only makes it searchable. Provider authentication belongs to the provider CLI; an available executable does not prove correct account access.

## Watch or schedule

Use `bf watch` as the primary refresh mode. Keep check/display periods and desktop notifications under `watch:` in `bf.yaml`; CLI options override it, then built-in defaults apply. Default alerts cover new failures and recovery, with a five-minute cooldown; success alerts are optional. Restart the collector after editing preferences. After adding a reviewed source to `bf.yaml`, press `f` (`u` remains an alias) to reload sources and check due work now. Refresh queues one follow-up if an update is active and respects pause, selectors, refresh intervals and failure backoff; observers only reread local state. A second watcher observes the active collector, while timers require explicit `bf status --watch`. See the [watch preferences](https://fmind.github.io/brain-framework/docs/schedule/#watch-preferences) and the runnable offline `examples/watch/` in the framework repository.

With recurring-execution authority, run `bf watch`; repeat `--sensor NAME` and `--routine NAME` to restrict work. If any selectors are supplied, only named programs run. The person at the terminal drives the dashboard with the [watch keys](https://fmind.github.io/brain-framework/docs/schedule/#watch-collection); both dashboards [sort in-session](https://fmind.github.io/brain-framework/docs/schedule/#sort-the-dashboard). Agents read state without executing anything through `bf status`, or stream `bf watch --json` rows when authorized to collect: `records` counts the last successful sensor run's returned items, not the stored catalog, rows carry no `observing` key, and times end in `Z` or are empty strings when absent (never succeeded, manual or disabled). A row's `status` is `failed`, `never`, `due`, `fresh`, `manual` or `disabled`. Keep one execution owner per selected program in each brain; separate brains may have their own collectors. For a shared source, designate one collecting machine so clones do not duplicate collection.

`bf schedule --brain PATH --sensor SENSOR --output settings/schedules` writes native files and returns installation, status and removal argv lists; omitting `--output` only previews them. A relative `--output` resolves against the brain root, so the command works from any directory. Generated files capture this machine's `PATH`, home and brain paths: keep them out of a shared brain's Git, for example with `/settings/schedules/` in its `.gitignore`. Review the files and activate them only with recurring-execution authority. Keep them outside `bf.yaml`; arbitrary scripts use native scheduler files directly. Check more often than the shortest `refresh`. Report generated, enabled and observed runs separately. See [Watch and schedule updates](https://fmind.github.io/brain-framework/docs/schedule/).

## Windows and freshness

For mutable window sources, use a short `refresh` and `overlap`, plus `reconcile: {refresh: 86400, lookback: 604800}` when a daily seven-day revisit is justified. Keep reconciliation on the same source; avoid widening every sensor request internally. Preview the planned bounds with `bf update --dry-run`. Compare `last_run` change counts, `elapsed_seconds` and `output_bytes`; stdout bytes do not measure provider traffic. A failed program retries after 1 minute, doubling per consecutive failure up to its `refresh`; `bf collect SENSOR` retries a sensor immediately, while a routine waits for its next backoff. Finite horizons do not guarantee discovery of older edits or deletions. See [update rules](https://fmind.github.io/brain-framework/docs/sensors/#collect-and-update).

A backfill ending before the recorded coverage leaves `last_collected` unchanged. For a scheduled window sensor, a later window separated from the coverage by a gap does not move the resume point; a manual sensor's latest run sets its `last_collected` and `window`, so backfill any gap explicitly. See [backfills and coverage](https://fmind.github.io/brain-framework/docs/sensors/#backfills-and-coverage).

Compare expected source coverage with actual retained records and `last_run`, then search/read a representative item. A fresh cache, successful local run or current dashboard is not proof of complete provider truth. Report planned, generated, enabled and observed operations separately.

## Refresh brain instructions

`bf init` writes a brain's `AGENTS.md` once. After installing a new BF version, compare it with the current template:

```bash
scratch="$(mktemp -d)"
bf init "$scratch/brain" --name NAME
diff "$scratch/brain/AGENTS.md" PATH/AGENTS.md
rm -r "$scratch"
```

No output means the instructions match. Otherwise merge the new guidance by hand, keep the owner's additions, run `bf validate` and show the diff. See [refresh brain instructions](https://fmind.github.io/brain-framework/docs/agents/#refresh-brain-instructions).

## Selected GitHub history

Use the reviewed [GitHub history walkthrough](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md) for one year of commits reachable from `main` and all-age open/closed issues and PRs. Confirm the repository and branch; use distinct window sources per repository. The year is an initial collection bound, not automatic deletion. Issue/PR windows select their latest modification; descriptions and state do not include comments, reviews or every historical revision.

Backfill adjacent bounded windows through the present, recording fixed endpoints and successful counts/bytes in a private checklist. Resume the first unfinished interval; retry or split failed intervals without skipping them. Replays use stable IDs. Keep incremental refresh plus periodic reconciliation afterward, with one execution owner per source. A new machine has new local run state: synced records alone do not establish its backfill coverage. Verify saved refs and retrieval after collection, and distinguish complete requested windows from provider completeness.
