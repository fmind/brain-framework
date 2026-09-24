---
name: fkf-maintain
description: Maintain FKF bases - collector health, scheduled updates, backfills, validation and retrieval cases. Use when fkf status reports stale or failing sources, when adding a collector, or when setting up a schedule.
license: MIT
---

# fkf-maintain

Select the intended base before maintenance; use `--base NAME` so the working directory cannot broaden the operation. Start with offline diagnosis. Live `update` and `collect` require the user's authorization; `collect --dry-run` still contacts the provider and writes its private stderr log.

```bash
fkf status --base NAME             # cache, sources, last success, errors, log paths
fkf update --dry-run --base NAME   # due sources and their windows, runs nothing
fkf update --base NAME             # when authorized: collect due sources, refresh cache
fkf validate --base NAME && fkf eval --base NAME
```

1. **Failing source**: read the log path from `fkf status`, reproduce with `fkf collect SOURCE --since 1d --dry-run --base NAME`, fix the collector under `sources/` with a fake-provider test under `tests/`, then rerun. Provider authentication belongs to the provider CLI (`gh auth`, `gws auth`).
1. **Stale source**: check the scheduler (`systemctl --user list-timers fkf-update.timer`, `journalctl --user -u fkf-update`) before blaming the collector. A paused laptop catches up at most 30 days automatically; backfill older gaps with `fkf collect SOURCE --since YYYY-MM-DD`.
1. **New collector**: add it only for a question the user asks repeatedly. Copy an example from the FKF repository's `examples/sources/`, keep projection focused (title and text carry the searchable facts, skip noise such as trash or bots), test it with a fake provider, declare a disabled source with explicit account/folder/channel scope, modification time, partial-content and deletion behavior, try `fkf collect NAME --dry-run` when enabled and authorized, then declare `refresh` in `fkf.yaml`.
1. **Usage**: once a month, read `usage` in `fkf status`. Near-zero searches mean agents are not reaching the base: check that `fkf-use` is installed where they run before improving anything else. A high share of `empty` searches means notes or collectors miss what people ask.
1. **Stale notes**: `review` in `fkf status` lists active or blocked projects whose note is older than 14 days; refresh them from recent records or set their verified current status.
1. **Incomplete retrieval**: inspect `problems` and `stale`, repair the named file or base, then repeat the query; `eval` rejects incomplete answers.
1. **Duplicate records**: collection refuses a source that already contains duplicate IDs. Run `fkf validate`, preserve the conflicting revisions and reconcile their evidence before retrying; never discard a revision simply to make collection succeed.
1. **Retrieval miss**: add the question as a case in `queries.yaml`, then improve the owning note or the collector's projection until `fkf eval` passes. Do not tune the core for one query.
1. **Schedule**: run `fkf update` more often than the shortest `refresh` (for example every 15 minutes for hourly sources) from a systemd user timer or launchd agent with the provider CLIs on PATH; see the FKF documentation's collector page. Report configured, enabled and observed runs separately.

Inspect active/disabled/historical coverage and `last_run` counts before interpreting freshness. Preserve `records/.pending/` during recovery and backup; it holds durable originals for interrupted writes. Inspect the base, then run `fkf build` for explicit recovery; search and read do not apply pending journals. Keep one scheduler per machine.

Never delete records, task inputs or outputs to fix a problem; `.fkf/` is the only disposable folder (`fkf build` recreates it). Collection runs code with the user's permissions: run live providers only within the user's authorization, and trust a shared base with `fkf register PATH --collect` only after reviewing its `sources/`.
