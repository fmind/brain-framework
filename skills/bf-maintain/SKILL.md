---
name: bf-maintain
description: Maintain Brain Framework brains - sensor health, scheduled updates, backfills, validation and retrieval cases. Use when bf status reports stale or failing sensors, when adding a sensor, or when setting up a schedule.
license: MIT
---

# bf-maintain

Select the intended brain before maintenance; use `--brain NAME` so the working directory cannot broaden the operation. Start with offline diagnosis. Live `update` and `collect` require the user's authorization; `collect --dry-run` still contacts the provider and writes its private stderr log.

```bash
bf status --brain NAME             # cache, sources, last success, errors, log paths
bf update --dry-run --brain NAME   # due sensors and their windows, runs nothing
bf update --brain NAME             # when authorized: collect due sensors, refresh cache
bf validate --brain NAME && bf eval --brain NAME
```

1. **Failing sensor**: read the log path from `bf status`, reproduce with `bf collect SENSOR --since 1d --dry-run --brain NAME`, fix the sensor under `sensors/` with a fake-provider test under `tests/`, then rerun. Provider authentication belongs to the provider CLI (`gh auth`, `gws auth`).
1. **Stale sensor**: check the scheduler (`systemctl --user list-timers bf-update.timer`, `journalctl --user -u bf-update`) before blaming the sensor. A paused laptop catches up at most 30 days automatically; backfill older gaps with `bf collect SENSOR --since YYYY-MM-DD`.
1. **New sensor**: add it only for a question the user asks repeatedly. Copy an example from the Brain Framework repository's `examples/sensors/`, keep projection focused (title and text carry the searchable facts, skip noise such as trash or bots), test it with a fake provider, declare it disabled with explicit account/folder/channel scope, modification time, partial-content and deletion behavior, try `bf collect NAME --dry-run` when enabled and authorized, then declare `refresh` in `bf.yaml`.
1. **Usage**: once a month, read `usage` in `bf status`. Near-zero searches mean agents are not reaching the brain: check that `bf-use` is installed where they run before improving anything else. A high share of `empty` searches means notes or sensors miss what people ask.
1. **Stale notes**: `review` in `bf status` lists active or blocked projects whose note is older than 14 days; refresh them from recent records or set their verified current status.
1. **Incomplete retrieval**: inspect `problems` and `stale`, repair the named file or brain, then repeat the query; `eval` rejects incomplete answers.
1. **Duplicate records**: collection refuses a source that already contains duplicate IDs. Run `bf validate`, preserve the conflicting revisions and reconcile their evidence before retrying; never discard a revision simply to make collection succeed.
1. **Retrieval miss**: add the question as a case in `evals/retrieval.yaml`, then improve the owning note or the sensor's projection until `bf eval` passes. Do not tune the core for one query.
1. **Schedule**: run `bf update` more often than the shortest `refresh` (for example every 15 minutes for hourly sources) from a systemd user timer or launchd agent with the provider CLIs on PATH; see the Brain Framework documentation's sensor page. Report configured, enabled and observed runs separately.

Inspect active/disabled/historical coverage and `last_run` counts before interpreting freshness. Preserve `memories/.pending/` during recovery and backup; it holds durable originals for interrupted writes. Inspect the brain, then run `bf build` for explicit recovery; search and read do not apply pending journals. Keep one scheduler per machine.

Never delete records, action inputs or outputs to fix a problem; `.bf/` is the only disposable folder (`bf build` recreates it). Collection runs code with the user's permissions: run live providers only within the user's authorization, and trust a shared brain with `bf register PATH --collect` only after reviewing its `sensors/`.

Shared field meanings, types, cardinality and examples live under `schema` in `bf.yaml`; sensor `fields` map explicit output paths or constants into them. Use `bf search --relation ROLE --target IDENTITY` to follow a typed relationship and read its supporting record. Keep technical checks in `tests/` and retrieval suites in `evals/`; `bf eval` runs all suites, while `--path evals/NAME.yaml` selects one.

## Portable links and relationships

Use stable `bf://<bf.yaml name>/...` addresses across brains. An authored note may declare `entity: bf://NAME/people/ID` (or another logical namespace), with verified alternate identities in `aliases`. Declare each role in `bf.yaml` (`type: identity`, `relation: true`) before writing `[label](bf://NAME/path?rel=ROLE#section)`. The subject defaults to the note entity, otherwise its file; use `subject=IDENTITY` when stating a relationship between other entities. Keep authorship and ownership in named relationship fields, not URI userinfo. Query values must be percent-encoded, and query attributes belong before the fragment. Other URI schemes retain their original query semantics.

Find incoming links with `bf search --target IDENTITY`, outgoing claims with `--subject IDENTITY`, and add `--relation ROLE` when needed. Read the returned `relations[].origin` and `evidence`; these retain the actual assertion and its cited support separately. Exact reads accept BF addresses. Use explicit heading anchors (`## Display title {#stable-id}`) when a section needs a durable ref. Keep the brain's `name` stable across clones. Selected-brain scope is a boundary: links never add another brain or contact a network. Ambiguous aliases and incomplete searches need review; `bf validate` reports foreign links under `unresolved` without opening them.

Related brains belong in `bf.yaml` as `brains: {team: {path: ../team}}`. Use stable matching names and paths relative to the declaring root. Search/read include direct references only; check `problems` before claiming absence. References never grant sensor execution permission.
