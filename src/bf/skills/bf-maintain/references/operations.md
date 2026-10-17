# Collection recovery and ongoing refresh

Use for failed or overdue collection, backfills, interrupted writes, watch and schedules, upgrades or brain instructions. Run commands inside the selected brain or with `--brain PATH`, which unattended jobs always pass. Live commands run configured code with the user's permissions and need authority covering the selected providers and scope.

## Read bf status

Agents read `bf status`, a JSON snapshot that runs nothing, never the interactive `bf status --watch`; the [status reference](https://fmind.github.io/brain-framework/docs/commands/#status-sources-and-routines) lists every field. Start from `attention`: scheduled programs that failed, are `overdue` (no success within twice their `refresh`) or `never` succeeded. Then:

- `cache` `busy` means another writer works, and replies carry `stale` meanwhile; `stale` or `missing` with `pending_transaction: true` means an interrupted record write awaits recovery.
- `runtime` names the `installed` version and the `pinned` one the brain's `uv.lock` names, if any.
- A sensor's `freshness` counts only collections that reached the present (`last_collected`); `last_run` counts the last committed run, backfills included. `manual` means no `refresh`, `unknown` a disabled program or a `historical` source, kept after its sensor left `bf.yaml`.
- An assistant appears under `assistants`, with its `runs`, `last_success` and latest `record`, and under `sources`, with its records.
- A failed program shows its `error`, consecutive `failures` and `log`.

`bf status --check` exits 1 on a scheduled program that failed or is `overdue` or `never`, on problems, a `stale` or `missing` cache, an unavailable brain or an unreadable `brains:` reference.

## Diagnose and recover

| Symptom                      | Next check                                                                                                                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Failed sensor or routine     | Read `logs/NAME.log` locally; within live authority, reproduce with `bf collect SENSOR --since 1d --dry-run` or `bf run --dry-run ROUTINE`. Add a fake-provider regression before retrying. |
| Failed assistant             | Read `logs/NAME.log`. `bf run --dry-run ASSISTANT` runs the full harness with every permission it has: reproduce only within live authority.                                                |
| `overdue` or `never` program | Check the watcher or scheduler and its logs. Catch-up covers at most 30 days: an older gap needs `bf collect SENSOR --since YYYY-MM-DD`.                                                    |
| Snapshot removal rejected    | `snapshot would remove N of M records`: check account, scope and mounts with `bf collect SENSOR --dry-run`; for an intended removal, run `bf collect SENSOR --allow-removal` once.          |
| Brain not found              | `PATH has no bf.yaml`: pass the brain's root. `registered brain directory is absent on this machine`: run `bf register` at its new place.                                                   |
| Interrupted write            | `pending_transaction: true`: preserve `memories/.pending/` and run `bf build`, or let the next `bf update`, recover it.                                                                     |
| Skipped files                | `problems` name the file; `bf build` and `bf update` exit 1 until it is fixed. Fix a skipped record's sensor, then collect a window returning it.                                           |
| Scan-limit warning           | A source or folder above 80% of its 100,000-entry limit: split the sensor or archive older records outside the brain before scans fail.                                                     |

`bf read memories/SOURCE` shows a source's coverage and latest records. The durable local assistant queue is not a cache. Only `.bf/` is disposable: preserve records, logs, action files and pending journals. Review a shared brain's `bf.yaml`, programs, prompts and pin files before running them. An available provider executable does not prove access to the right account.

## Watch or schedule

`bf watch` is the primary refresh mode: it runs due sensors, then routines, with independent assistant workers and channel streams, until the user quits; `--sensor NAME`, `--routine NAME` or `--assistant NAME` restrict it. Restart it after editing its `watch:` preferences in `bf.yaml`. Agents observe with `bf status`, or stream `bf watch --json` when authorized to collect. Keep one execution owner per program and one collecting machine per shared source ([watch and schedule](https://fmind.github.io/brain-framework/docs/schedule/)).

With recurring-execution authority, `bf schedule --sensor SENSOR --output ~/.config/bf-schedules` writes native systemd, launchd or cron files and returns the commands to activate, inspect and remove them; it activates nothing. The files capture this machine's `PATH`: keep them out of a shared brain's Git, generate them through the pin for a pinned brain and regenerate them after an upgrade moves `bf`. Report generated, enabled and observed runs separately.

## Windows and freshness

For mutable window sources, use a short `refresh` and an `overlap`, plus `reconcile: {refresh: 86400, lookback: 604800}` when a daily seven-day revisit is justified; preview with `bf update --dry-run`. A failed sensor or routine retries after 1 minute, doubling up to its `refresh`. A backfill chunk ending before now extends `window` without making the source fresh: finish with `--since` and no `--until`, or let the next update run ([backfills and coverage](https://fmind.github.io/brain-framework/docs/sensors/#backfills-and-coverage)).

## Upgrade Brain Framework

Upgrade only with the user's consent. Stop watchers and schedules, read the release's changelog and back up the idle brain. Run `uv tool upgrade brain-framework` and, in a pinned brain the user created or reviewed, `uv add "brain-framework==X.Y.Z"` so `runtime.pinned` keeps the installed major version. Apply the changelog's manual steps, then:

```bash
bf --version
bf validate
bf eval
bf skills ~/.agents/skills --check
```

`bf skills DIR` then updates `outdated` and `missing` skills but leaves unchanged, exiting 1, a folder the user edited (`modified`), one BF did not install (`unmanaged`) and one a newer `bf`, such as a pinned runtime, installed (`newer`). Pass `--force` for the first two only when the user accepts; for `newer`, upgrade this `bf` instead. Regenerate schedules when the changelog asks, then restart watchers and agent hosts.

## Refresh brain instructions

`bf init` writes a brain's `AGENTS.md` once. After an upgrade, diff it against a scratch `bf init` copy, merge new guidance by hand, keep the owner's additions and run `bf validate` ([refresh brain instructions](https://fmind.github.io/brain-framework/docs/agents/#refresh-brain-instructions)).

## Selected GitHub history

Follow the reviewed [GitHub history walkthrough](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md), with one window source per confirmed repository. Backfill adjacent bounded windows through the present, recording endpoints and counts in a private checklist, and split failed intervals without skipping them. Synced records do not establish a new machine's backfill coverage.
