---
description: Configure an offline routine that turns saved evidence into a review action.
---

# Prepare reviews

A registered routine prepares a review action from local evidence. For example, the weekly review reads your projects and recent activity, then creates an `ACTION.md` with tasks for you or your agent. It uses no model and makes no decisions for you.

`bf update` runs due routines, validates their OKF Markdown output and saves each nonempty result as a new action. General upkeep scripts such as backups can also live in `routines/`; run those directly or through your own scheduler.

## Configure a routine

Complete [Getting started](getting-started.md) and work inside `~/brain`. Copy the [weekly review example](https://github.com/fmind/brain-framework/blob/main/examples/routines/weekly-review.py) from the release matching your installation, then review it. The Python package does not install these scripts.

```bash
mkdir -p routines
curl -fsSLo routines/weekly-review.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/routines/weekly-review.py"
```

A new brain has no `routines:` key. Add this one at the top level of `bf.yaml`, keeping the rest of your configuration:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [uv, run, --no-project, --python, "3.14", routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
```

This invocation supplies Python 3.14 and does not require an executable bit on the script. Both `uv` and `bf` must be on PATH; uv may obtain the interpreter on its first run. The routine itself reads local evidence without contacting providers.

The name becomes the action's slug: start with a lowercase letter, then use lowercase letters and digits separated by single hyphens. Sensor and routine names must be distinct.

| Setting     | Default  | Meaning                                                                  |
| ----------- | -------- | ------------------------------------------------------------------------ |
| `command`   | required | A command on PATH or a `routines/` executable, then up to 127 arguments. |
| `enabled`   | `true`   | Whether the routine may run.                                             |
| `refresh`   | `0`      | Seconds between runs, up to 365 days; zero leaves it out of updates.     |
| `lookback`  | `86400`  | Seconds covered by the first run; up to 365 days.                        |
| `timeout`   | `300`    | Maximum runtime in seconds, from 1 to 3,600.                             |
| `max_bytes` | 1 MiB    | Maximum stdout; configurable up to 4 MiB.                                |

Arguments support `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}`. After the first run, `start` is the end of the last window that produced an action or found nothing to review; `end` is now.

## Run and review

`bf update` runs due routines after the selected brain's sensors. Preview the due list, then create the review:

```bash
bf update --dry-run
bf update
bf read actions
bf validate
```

If there are projects due for review, open tasks or dated items from the last seven days, the update reply names the new `actions/YYYY-MM-DD_weekly-review-UUID/ACTION.md`. Read that returned path to see the review tasks; the date is the day you ran it. If there is nothing to review, the routine succeeds without creating an action. An immediate second update leaves existing work intact.

To preview the Markdown without saving an action, run the script directly with your review timestamp:

```bash
uv run --no-project --python 3.14 routines/weekly-review.py ~/brain 2026-09-27T12:00:00Z
```

The action includes open/completed task counts across notes and a preview of up to ten open tasks with their source sections and lines. Follow `bf read tasks` continuations for the complete list. Summarized tasks use plain bullets, so generating a review does not duplicate their checkboxes or add graph claims. The counts exclude deprecated notes and action attachments; incomplete task replies stop the routine before it writes.

The timestamp sets the note's date; the example reads the brain's current home, project, task and last-seven-days pages. It does not recreate the brain as it was on a past date.

A successful routine produces one of three outcomes:

| Output or existing state               | Result                                                                  |
| -------------------------------------- | ----------------------------------------------------------------------- |
| Valid OKF Markdown                     | A new `actions/YYYY-MM-DD_NAME-UUID/ACTION.md`, using the local date.   |
| Empty output                           | Success with no action.                                                 |
| Today's action for this routine exists | Skipped; existing work stays intact and the review window remains open. |

The routine recognizes today's action by its folder name, `actions/YYYY-MM-DD_NAME-UUID`, not by run history: a retry after an action was written but its history was not saved skips too, and so does an action another clone wrote and shared. Today's action counts while its folder holds any file, even an editor's `.#ACTION.md` lock, so a rerun never writes beside unsaved edits. Failures, invalid OKF metadata or Markdown, and excessive output create no action. The routine retries with the same [failure backoff](sensors.md#collect-and-update) as a sensor, and the failure appears in `bf status` and the home page's `attention`.

## Write a review routine

Use `bf read` and `bf search` with literal arguments to read the brain. Programs receive `BF_BRAIN` set to the brain running them, so a nested `bf` call without `--brain` reads that brain even when your shell selected another one. The weekly review makes this explicit by passing its `{{brain}}` argument to `bf read --brain`. Keep routines offline and deterministic; do not edit existing notes or call a model. Name collected evidence by ref so a reviewer can choose what to open.

For example, a routine reviewing the New website decision can print this complete action:

```markdown
---
type: action
status: draft
summary: Review the New website project before the next work session.
---

# New website review

## Objective

Review the [project decision](../../projects/new-website.md#decision).

## Tasks

- [ ] Check whether the product page now explains the product before signup.

## Resume

Read the project and its evidence, then record the review outcome.
```

Every nonempty output needs a nonempty `type`. Optional `status` must be `draft`, `stable` or `deprecated`; keep review progress in the task list. Relative links resolve from the resulting `ACTION.md`, two levels below the brain root. The routine emits the complete note, including its metadata.

Test the script with a fake `bf`. It runs with the same [process safeguards](limits.md#processes-and-logs) as a sensor. `bf status` reports its `state`, `freshness`, `last_success` and latest `action`, plus `failed`, `error`, `failures` and `log` after a failure; see [status fields](commands.md#status-sources-and-routines).

To run updates automatically, see [Watch and schedule updates](schedule.md).

Routine action folders follow the [action folder convention](brain.md#actions): a fresh UUID hex suffix. Independent clones create distinct sessions; this avoids filename conflicts but does not deduplicate overlapping reviews. Use one scheduler for a shared routine when only one team review is wanted. A clone remembers its last action in private run state and never replaces it.
