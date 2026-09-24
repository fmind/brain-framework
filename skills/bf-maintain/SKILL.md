---
name: bf-maintain
description: Maintain Brain Framework brains - sensor health, scheduled updates, backfills, validation and retrieval cases. Use when bf status reports stale or failing sources, when adding a sensor, or when setting up a schedule.
license: MIT
---

# bf-maintain

Select the intended brain before maintenance; use `--brain NAME` so the working directory cannot broaden the operation. Start with offline diagnosis. Live `update` and `collect` require the user's authorization; `collect --dry-run` still contacts the provider and writes its private stderr log.

```bash
bf status --brain NAME             # cache, sources, last success, errors, log paths
bf update --dry-run --brain NAME   # due sources and their windows, runs nothing
bf update --brain NAME             # when authorized: collect due sources, refresh cache
bf validate --brain NAME && bf eval --brain NAME
```

1. **Failing source**: read the log path from `bf status`, reproduce with `bf collect SENSOR --since 1d --dry-run --brain NAME`, fix the sensor under `sensors/` with a fake-provider test under `tests/`, then rerun. Provider authentication belongs to the provider CLI (`gh auth`, `gws auth`).
1. **Stale source**: check the scheduler (`systemctl --user list-timers bf-update.timer`, `journalctl --user -u bf-update`) before blaming the sensor. A paused laptop catches up at most 30 days automatically; backfill older gaps with `bf collect SENSOR --since YYYY-MM-DD`.
1. **New sensor**: add it only for a question the user asks repeatedly. Copy an example from the Brain Framework repository's `examples/sensors/`, keep projection focused (title and text carry the searchable facts, skip noise such as trash or bots), test it with a fake provider, declare a disabled source with explicit account/folder/channel scope, modification time, partial-content and deletion behavior, try `bf collect NAME --dry-run` when enabled and authorized, then declare `refresh` in `bf.yaml`.
1. **Usage**: once a month, read `usage` in `bf status`. Near-zero searches mean agents are not reaching the brain: check that `bf-use` is installed where they run before improving anything else. A high share of `empty` searches means notes or sensors miss what people ask.
1. **Stale notes**: `review` in `bf status` lists active or blocked projects whose note is older than 14 days; refresh them from recent records or set their verified current status.
1. **Incomplete retrieval**: inspect `problems` and `stale`, repair the named file or brain, then repeat the query; `eval` rejects incomplete answers.
1. **Duplicate records**: collection refuses a source that already contains duplicate IDs. Run `bf validate`, preserve the conflicting revisions and reconcile their evidence before retrying; never discard a revision simply to make collection succeed.
1. **Retrieval miss**: add the question as a case in `queries.yaml`, then improve the owning note or the sensor's projection until `bf eval` passes. Do not tune the core for one query.
1. **Schedule**: run `bf update` more often than the shortest `refresh` (for example every 15 minutes for hourly sources) from a systemd user timer or launchd agent with the provider CLIs on PATH; see the Brain Framework documentation's sensor page. Report configured, enabled and observed runs separately.

Inspect active/disabled/historical coverage and `last_run` counts before interpreting freshness. Preserve `memories/.pending/` during recovery and backup; it holds durable originals for interrupted writes. Inspect the brain, then run `bf build` for explicit recovery; search and read do not apply pending journals. Keep one scheduler per machine.

Never delete records, action inputs or outputs to fix a problem; `.bf/` is the only disposable folder (`bf build` recreates it). Collection runs code with the user's permissions: run live providers only within the user's authorization, and trust a shared brain with `bf register PATH --collect` only after reviewing its `sensors/`.
