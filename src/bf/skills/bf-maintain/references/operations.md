# Collection recovery and ongoing refresh

Use for failed or overdue collection, backfills, interrupted writes, watch and schedules, upgrades or brain instructions. Run commands inside the selected brain or with `--brain PATH`, which unattended jobs always pass. Live commands run configured code with the user's permissions and need authority covering the selected providers and scope.

## Read bf status

Agents read `bf status`, a JSON snapshot that runs nothing, never the interactive `bf status --watch`; the [status reference](https://fmind.github.io/brain-framework/docs/commands/#status-sources-and-routines) lists every field. Start from `attention`: scheduled programs that `failed`, are `overdue` (no success within twice their `refresh`) or `never` succeeded, and `silent` sources, which gained no new or changed record within their `silent_after`, whatever their freshness. Then:

- `cache` `busy` means another writer works, and replies carry `stale` meanwhile; `stale` or `missing` with `pending_transaction: true` means an interrupted record write awaits recovery.
- `runtime` names the `installed` version and the `pinned` one the brain's `uv.lock` names, if any.
- A sensor's `freshness` counts only collections that reached the present (`last_collected`); `last_run` counts the last committed run, backfills included. `manual` means no `refresh`, `unknown` a disabled program or a `historical` source, kept after its sensor left `bf.yaml`.
- An assistant appears under `assistants`, with its `runs`, `last_success` and latest `record`, and under `sources`, with its records. `attention` also names an assistant with failed or uncertain jobs or schedules that cannot start, and a failing `channel`; `messaging` holds the queue's jobs, deliveries and `transports` errors.
- A failed program shows its `error`, consecutive `failures` and `log`; a disabled one reports no failure.

`bf status --check` exits 1 on a scheduled program that failed or is `overdue` or `never`, on a `silent` source, on an assistant, unless disabled, with failed or uncertain jobs or schedules that cannot start, on a channel with an unresolved delivery that failed or is uncertain or a stream or read error (`bf queue` lists them), on problems, a `stale` or `missing` cache, an unavailable brain or an unreadable `brains:` reference.

## Diagnose and recover

| Symptom                      | Next check                                                                                                                                                                                                                                                                                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Failed sensor or routine     | Read `logs/NAME.log` locally; within live authority, reproduce with `bf collect SENSOR --since 1d --dry-run` or `bf run --dry-run ROUTINE`. Add a fake-provider regression before retrying.                                                                                                                      |
| Failed assistant             | Read `logs/NAME.log` and the error `bf queue` lists for its job or delivery; inspect `bf queue ID` before `--retry` or `--cancel`. `bf run --dry-run ASSISTANT` runs the full harness with every permission it has: reproduce only within live authority.                                                        |
| `overdue` or `never` program | Check the watcher or scheduler and its logs. Catch-up covers at most 30 days: an older gap needs `bf collect SENSOR --since YYYY-MM-DD`.                                                                                                                                                                         |
| `silent` source              | Runs succeed but bring nothing new: within live authority, check its account, filter or query with `bf collect SENSOR --dry-run`; raise `silent_after` only for a genuine quiet spell.                                                                                                                           |
| Snapshot removal rejected    | `snapshot would remove N of M records`: check account, scope and mounts with `bf collect SENSOR --dry-run`; for an intended removal, run `bf collect SENSOR --allow-removal` once.                                                                                                                               |
| Moved or renamed brain       | `assistant queue holds unresolved work admitted under this brain's previous path or name`: restore the old path or name, resolve the work with `bf queue`, then move again. A drained queue reports nothing and rebinds at the next `bf queue` or execution command.                                             |
| Queue from an older release  | `assistant queue predates this release`: preserve the queue and follow the changelog's upgrade steps; never delete it ([troubleshooting](https://fmind.github.io/brain-framework/docs/troubleshooting/)).                                                                                                        |
| Brain not found              | `PATH has no bf.yaml`: pass the brain's root. `registered brain directory is absent on this machine` or `bf.yaml is missing`: restore it, or run `bf register` at its new place.                                                                                                                                 |
| Interrupted write            | `pending_transaction: true`: preserve `memories/.pending/` and run `bf build`, or let the next `bf update` recover it. If `bf build` refuses the journal itself, follow [recover an interrupted write](https://fmind.github.io/brain-framework/docs/troubleshooting/#recover-the-cache-or-an-interrupted-write). |
| Skipped files                | `problems` name the file; `bf build` and `bf update` exit 1 until it is fixed. Fix a skipped record's sensor, then collect a window returning it.                                                                                                                                                                |
| Scan-limit warning           | A source or folder above 80% of its 100,000-entry limit: split the sensor or archive older records outside the brain before scans fail.                                                                                                                                                                          |

`bf read memories/SOURCE` shows a source's coverage and latest records. The durable local assistant queue is not a cache. Only `.bf/` is disposable: preserve records, logs, action files and pending journals. Review a shared brain's `bf.yaml`, programs, prompts, pin files and the agent configuration an assistant's harness loads from the brain folder, such as `.claude/`, `.mcp.json`, `AGENTS.md` and `CLAUDE.md`, before running them. An available provider executable does not prove access to the right account.

## Watch or schedule

`bf watch` is the primary refresh mode: it runs due sensors, then routines, with independent assistant workers and channel streams, until the user quits; `--sensor NAME`, `--routine NAME` or `--assistant NAME` restrict it. Restart it after editing its `watch:` preferences, adding or renaming an assistant, or changing stream subscriptions or channel commands in `bf.yaml`. Agents observe with `bf status`, or stream `bf watch --json` when authorized to collect. Keep one execution owner per program and one collecting machine per shared source ([watch and schedule](https://fmind.github.io/brain-framework/docs/schedule/)).

With recurring-execution authority, propose a systemd user service or LaunchAgent running `bf watch --json --brain PATH`, written from the [service guide](https://fmind.github.io/brain-framework/docs/schedule/#run-watch-as-a-service): absolute paths, a `PATH` that finds `bf` and every provider CLI the programs call, and the user's `XDG_CONFIG_HOME` and `XDG_STATE_HOME` when set. Keep the file out of a shared brain's Git, run it through the pin for a pinned brain and edit it when an upgrade moves `bf`. Install or enable it only with the user's consent. Report written, enabled and observed runs separately.

## Windows and freshness

For mutable window sources, use a short `refresh` and an `overlap`, plus `reconcile: {refresh: 86400, lookback: 604800}` when a daily seven-day revisit is justified; preview with `bf update --dry-run`. A failed sensor or routine retries after 1 minute, doubling up to its `refresh`. A backfill chunk ending before now extends `window` without making the source fresh: finish with `--since` and no `--until`, or let the next update run ([backfills and coverage](https://fmind.github.io/brain-framework/docs/sensors/#backfills-and-coverage)).

## Upgrade Brain Framework

Upgrade only with the user's consent. Stop watchers and schedules, read the release's changelog and back up the idle brain with its local assistant queue (`$XDG_STATE_HOME/bf/runtime/`); resolve `bf queue` work before changing assistant or channel configuration. Run the changelog's steps that need the previous release, such as draining the queue, before upgrading. Run `uv tool upgrade brain-framework`, which keeps an installed `mcp` extra; if a host starts `bf mcp` and `uv tool list --show-extras` shows no `[extras: mcp]`, install `'brain-framework[mcp]'` instead. In a pinned brain the user created or reviewed, run `uv add "brain-framework==X.Y.Z"` so `runtime.pinned` keeps the installed major version. Apply the remaining manual steps, then:

```bash
bf --version
bf validate
bf eval
bf status --check
bf skills ~/.agents/skills --check
```

`bf skills DIR` then updates `outdated` and `missing` skills but leaves unchanged, exiting 1, a folder the user edited (`modified`), one BF did not install (`unmanaged`) and one a newer `bf`, such as a pinned runtime, installed (`newer`). Pass `--force` for the first two only when the user accepts; for `newer`, upgrade this `bf` instead. Update the watch service or timer the user wrote when the changelog asks or the `bf` path changes, then restart watchers and agent hosts.

## Refresh brain instructions

`bf init` writes a brain's `AGENTS.md` once. After an upgrade, diff it against a scratch `bf init` copy, merge new guidance by hand, keep the owner's additions and run `bf validate` ([refresh brain instructions](https://fmind.github.io/brain-framework/docs/agents/#refresh-brain-instructions)).

## Selected GitHub history

Follow the reviewed [GitHub history walkthrough](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md), with one window source per confirmed repository. Backfill adjacent bounded windows through the present, recording endpoints and counts in a private checklist, and split failed intervals without skipping them. Synced records do not establish a new machine's backfill coverage.
