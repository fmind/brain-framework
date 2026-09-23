---
name: fkf-maintain
description: Maintain FKF bases - collector health, scheduled updates, backfills, validation and retrieval cases. Use when fkf status reports stale or failing sources, when adding a collector, or when setting up a schedule.
license: MIT
---

# fkf-maintain

```bash
fkf status              # every base: cache, sources, last success, errors, log paths
fkf update --dry-run    # due sources and their windows, runs nothing
fkf update              # collect due sources of trusted bases, refresh caches
fkf validate --base NAME && fkf eval --base NAME
```

1. **Failing source**: read the log path from `fkf status`, reproduce with `fkf collect SOURCE --since 1d --dry-run --base NAME`, fix the collector under `sources/` with a fake-provider test under `tests/`, then rerun. Provider authentication belongs to the provider CLI (`gh auth`, `gws auth`).
1. **Stale source**: check the scheduler (`systemctl --user list-timers fkf-update.timer`, `journalctl --user -u fkf-update`) before blaming the collector. A paused laptop catches up at most 30 days automatically; backfill older gaps with `fkf collect SOURCE --since YYYY-MM-DD`.
1. **New collector**: add it only for a question the user asks repeatedly. Copy an example from the FKF repository's `examples/sources/`, keep projection focused (title and text carry the searchable facts, skip noise such as trash or bots), test it with a fake provider, try `fkf collect NAME --dry-run`, then declare `refresh` in `fkf.yaml`.
1. **Retrieval miss**: add the question as a case in `queries.yaml`, then improve the owning note or the collector's projection until `fkf eval` passes. Do not tune the core for one query.
1. **Schedule**: run `fkf update` hourly from a systemd user timer or launchd agent with the provider CLIs on PATH; see the FKF documentation's collector page. Report configured, enabled and observed runs separately.

Never delete records, task inputs or outputs to fix a problem; `.fkf/` is the only disposable folder (`fkf build` recreates it). Collection runs code with the user's permissions: run live providers only within the user's authorization, and trust a shared base with `fkf register PATH --collect` only after reviewing its `sources/`.
