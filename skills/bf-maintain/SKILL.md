---
name: bf-maintain
description: Maintain Brain Framework brains - sensor and routine health, scheduled updates, backfills, validation, retrieval cases and Git conflict resolution. Use when bf status or the home page reports stale or failing sensors or routines, when adding a sensor or routine, when setting up a schedule, or when concurrent brain revisions conflict.
license: MIT
compatibility: Requires Brain Framework 13 (the bf command) on Linux or macOS.
metadata:
  version: "13.0.0"
---

# bf-maintain

For first-use onboarding, use [bf-setup](../bf-setup/SKILL.md); for approved source discovery, use [bf-scan](../bf-scan/SKILL.md). This skill owns implementing and operating their selected sensors and routines. Carry forward the recurring question, account/folder/repository scope, retained fields, exclusions, access gaps and freshness need. Discovery approval alone does not authorize provider execution; reuse explicit implementation and live-run authority already given.

Select the intended brain before maintenance; use `--brain NAME` so the working directory cannot broaden the operation. Start with offline diagnosis. Live `update` and `collect` require the user's authorization; `collect --dry-run` still contacts the provider and writes its private stderr log.

```bash
bf status --brain NAME             # cache, sources, routines, last success, errors, log paths
bf update --dry-run --brain NAME   # due sensors and routines and their windows, runs nothing
bf update --brain NAME             # when authorized: collect due sensors, refresh cache
bf validate --brain NAME && bf eval --brain NAME
```

1. **Failing sensor**: read the log path from `bf status`, reproduce with `bf collect SENSOR --since 1d --dry-run --brain NAME`, fix the sensor under `sensors/` with a fake-provider test under `tests/`, then rerun. Provider authentication belongs to the provider CLI (`gh auth`, `gws auth`).
1. **Stale sensor**: inspect the active watch or native scheduler before blaming the sensor. `bf status --watch` observes local history; inspect the owning service's logs when it runs in the background. A paused laptop catches up at most 30 days automatically; backfill older gaps with `bf collect SENSOR --since YYYY-MM-DD`.
1. **New sensor**: add it only for a question the user asks repeatedly. Copy an example from the Brain Framework repository's `examples/sensors/`, keep projection focused (title and text carry the searchable facts, skip noise such as trash or bots), test it with a fake provider, declare it disabled with explicit account/folder/channel scope, modification time, partial-content and deletion behavior, try `bf collect NAME --dry-run` when enabled and authorized, then declare `refresh` in `bf.yaml`.
1. **New routine**: add one only for a review the user repeats. Copy an example from the Brain Framework repository's `examples/routines/` into `routines/`, keep it deterministic (it reads `bf read`/`bf search` pages and prints OKF Markdown with `type: action` and `status: draft`, or nothing when there is nothing to review; no model, network or provider call), test it with a fake `bf`, and declare it under `routines:` with a `refresh`. `bf update` validates the output and writes `actions/YYYY-MM-DD_NAME-UUID/ACTION.md` after the sensors, never replacing an existing action. Preview it by running the script directly. A failing routine writes nothing, stays due and appears under `attention` on the home page.
1. **Usage**: once a month, read `usage` in `bf status`. Near-zero searches mean agents are not reaching the brain: check that `bf-use` is installed where they run before improving anything else. A high share of `empty` searches means notes or sensors miss what people ask.
1. **Notes needing attention**: inspect `review_reasons`, `modified`, `review_due` and `new_links` on `bf read projects`. Reminders default to 14 days after the file's local modification time; optional `review_after` days or a `review_due` date customize them. Copies/checkouts can reset modification times. A reminder or recent edit establishes no verification. Read the note and evidence before changing its conclusion or deadline. Use `bf read tasks` for all open work and counts; keep generated summaries as plain bullets so they do not create duplicate tasks.
1. **Incomplete retrieval**: inspect `problems` and `stale`, repair the named file or brain, then repeat the query; `eval` rejects incomplete answers.
1. **Empty snapshot**: collection refuses to replace a non-empty catalog with an empty one, since a wrong account or an unmounted folder also looks empty. Check the sensor's scope and provider authentication; delete `memories/SOURCE/` only when the user confirms the catalog is really empty.
1. **Duplicate records**: collection refuses a source that already contains duplicate IDs. Run `bf validate`, preserve the conflicting revisions and reconcile their evidence before retrying; never discard a revision simply to make collection succeed.
1. **Retrieval miss**: add the question as a case in `evals/retrieval.yaml`, then improve the owning note or the sensor's projection until `bf eval` passes. Do not tune the core for one query.
1. **Watch**: `bf status --watch --brain NAME` observes local run history without execution. With collection authority, `bf watch --brain NAME` runs due programs until exit; select specific programs with repeatable `--sensor NAME` and `--routine NAME`. If any selectors are supplied, only named programs run. Space pauses future checks, `q` cancels the active update and quits, and `?` explains the display. Do not mistake fresh local history for provider truth.
1. **Schedule**: `bf schedule --brain NAME --sensor SENSOR --output settings/schedules` writes native files and returns installation, status and removal argv lists; omitting `--output` only previews them. Review the generated files and activate them only with recurring-execution authority. Keep the files alongside other brain-owned native jobs, outside `bf.yaml`; arbitrary scripts use native scheduler files directly. Check more often than the shortest `refresh`. Report generated, enabled and observed runs separately. See [Watch and schedule updates](https://fmind.github.io/brain-framework/docs/schedule/).

Inspect active/disabled/historical coverage and `last_run` counts before interpreting freshness; `bf read memories` and `bf read memories/SOURCE` show each source's coverage and paginated latest records. Preserve `memories/.pending/` during recovery and backup; it holds durable originals for interrupted writes. Inspect the brain, then run `bf build` for explicit recovery; search and read do not apply pending journals. Keep one scheduler per machine.

For mutable window sources, use a short `refresh` and `overlap`, plus `reconcile: {refresh: 86400, lookback: 604800}` when a daily seven-day revisit is justified. Keep reconciliation on the same source; avoid widening every sensor request internally. Preview the planned bounds with `bf update --dry-run`. Compare `last_run` change counts, `elapsed_seconds` and `output_bytes`; stdout bytes do not measure provider traffic. Failed runs remain due, and finite horizons do not guarantee discovery of older edits or deletions.

Never delete records, action inputs or outputs to fix a problem; `.bf/` is the only disposable folder (`bf build` recreates it). Collection and routines run code with the user's permissions: run live providers only within the user's authorization, and review a shared brain's `bf.yaml`, `sensors/` and `routines/` before running or scheduling updates. Registration only selects brains by name; use an explicit `--brain PATH` in schedules.

Shared field meanings, types, cardinality and examples live under `schema` in `bf.yaml`; sensor `fields` map explicit output paths or constants into them. Sensors emit explicit identities only (`person:email/ADDRESS`, `repo:github.com/OWNER/NAME`), never names matched by similarity. Keep technical checks in `tests/` and retrieval suites in `evals/`; `bf eval` runs all suites, while `--path evals/NAME.yaml` selects one.

Related brains belong in `bf.yaml` as `brains: {team: {path: ../team}}`. Maintenance commands act on the selected root only; references never grant sensor execution permission. `bf validate` reports foreign links under `unresolved` without opening them. Follow `bf-learn` to author entities and typed links.

For merge conflicts or competing identities, follow the [resolution guide](references/conflicts.md). Preserve both revisions until evidence supports a resolution; validate the merged result before delivery.

Use `bf watch` as the primary refresh mode. Keep check/display periods and desktop notifications in `settings/watch.yaml`; CLI options override it, then built-in defaults apply. Default alerts cover new failures and recovery, with a five-minute cooldown; success alerts are optional. Restart the collector after editing preferences. A second watcher observes the active collector, while timers require explicit `bf status --watch`. Keep one execution owner; `bf schedule` remains an optional generator of native files. See the [watch preferences](https://fmind.github.io/brain-framework/docs/schedule/#watch-preferences) and the runnable offline `examples/watch/` in the framework repository.
