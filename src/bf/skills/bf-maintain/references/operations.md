# Collection recovery and ongoing refresh

Use for unhealthy status, backfills, interrupted writes, `bf watch` services or upgrades. Always pass `--brain PATH` in unattended commands, and execute only within explicit authority.

## Read bf status {#read-bf-status}

Read `bf status` (offline JSON snapshot matching `bf schema --kind status-reply`, never interactive `bf status --watch`; [status reference](https://fmind.github.io/brain-framework/docs/commands/#status-sources-and-routines)). Start from uniform `attention` entries (`{kind, name, freshness, failed, ...}` where `kind` is `sensor`, `routine`, `assistant` or `channel`):

- `failed` programs or channels, `overdue` programs (past twice `refresh`, or 24h past `at`), programs due that `never` succeeded, `silent` sources (no new/changed record within `silent_after`) and `degraded` sources (3+ consecutive runs reporting [partial coverage](https://fmind.github.io/brain-framework/docs/sensors/#report-partial-coverage)).
- `cache`: `busy` means an active writer (reads return `stale`); `stale` or `missing` with `pending_transaction: true` awaits `bf build`.
- `runtime`: compares `installed` against any `pinned` version in `uv.lock`; `messaging` summarizes queue jobs, deliveries, `transports` errors and per-slot `wakes`.

`bf status --check` exits 1 whenever `healthy` is false (`problems`, `attention`, `stale`/`missing` cache, or unavailable brain/`brains:` target).

## Diagnose and recover

| Symptom                   | Action                                                                                                                                                                                                                                                                       |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `failed` sensor/routine   | Inspect `logs/NAME.log` locally; within live authority test with `bf collect SENSOR --since 1d --dry-run` or `bf run --dry-run ROUTINE` and add a fixture test.                                                                                                              |
| `failed` assistant/queue  | Inspect `logs/NAME.log` and `bf queue ID` before `--retry` or `--cancel` (periodic `failed` schedules self-heal next window; `uncertain` jobs always wait for inspection; [changed files](https://fmind.github.io/brain-framework/docs/prompts/#review-what-a-run-changed)). |
| `overdue` or `never`      | Check `bf watch` or scheduler logs; automatic catch-up spans up to 30 days, so backfill older gaps via `bf collect SENSOR --since YYYY-MM-DD` ([backfills](https://fmind.github.io/brain-framework/docs/sensors/#backfills-and-coverage)).                                   |
| `silent` or `degraded`    | `silent`: test filter/account with `bf collect SENSOR --dry-run` before raising `silent_after`. `degraded`: fix missed feeds/accounts named in `logs/NAME.log`.                                                                                                              |
| Snapshot removal blocked  | Verify scope/mounts with `bf collect SENSOR --dry-run`; pass `--allow-removal` once when intended.                                                                                                                                                                           |
| Moved/legacy queue        | `assistant queue holds unresolved work admitted under this brain's previous path or name`: restore path/name, drain `bf queue`, then move. `assistant queue predates this release`: follow changelog upgrade steps; never delete runtime state.                              |
| Interrupted write / skips | `pending_transaction: true`: run `bf build` ([recovery](https://fmind.github.io/brain-framework/docs/troubleshooting/#recover-the-cache-or-an-interrupted-write)). For skipped records in `problems`, fix the sensor and re-collect.                                         |

Only `.bf/` is disposable: preserve `memories/`, `memories/.pending/`, `actions/`, `logs/` and `$XDG_STATE_HOME/bf/runtime/`. Review a shared brain's `bf.yaml`, programs, prompts and harness configs (`.claude/`, `.mcp.json`, `AGENTS.md`) before executing anything.

## Watch, windows and upgrades

- **Watch**: `bf watch` runs due sensors, routines (interval or `at`), assistant workers and channel streams ([service guide](https://fmind.github.io/brain-framework/docs/schedule/#run-watch-as-a-service)); restart after changing `watch:`, assistants or channel transports. For mutable window sources, pair `refresh` + `overlap` with `reconcile: {refresh: 86400, lookback: 604800}`; for GitHub backfills, follow [github-history.md](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md).
- **Upgrade `bf`**: with user consent, stop watchers, back up the brain and `$XDG_STATE_HOME/bf/runtime/`, drain `bf queue` if required by the changelog, and run `uv tool upgrade brain-framework` (or `'brain-framework[mcp]'`; update reviewed pins with `uv add "brain-framework==X.Y.Z"`). Then verify:

```bash
bf --version
bf validate
bf eval
bf status --check
bf skills ~/.agents/skills --check
```

Run `bf skills DIR` (or `bf skills skills --link` inside a brain) to update untouched skills; `modified`, `unmanaged` and `newer` exit 1 (`--force` replaces the first two with consent; upgrade `bf` for `newer`). Refresh the text above `## Your rules` in `AGENTS.md` from a scratch `bf init` copy ([refresh instructions](https://fmind.github.io/brain-framework/docs/agents/#refresh-brain-instructions)).
