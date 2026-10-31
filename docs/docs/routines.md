---
icon: lucide/repeat
description: Run deterministic brain programs now, on a schedule or from Git hooks, and turn their output into logs or review actions.
---

# Run routines

A routine is any deterministic program your brain runs: a validation, a backup, a weekly review. It uses no model and makes no decisions for you; run your agent harness as an [assistant](prompts.md) instead. Declare it under `routines:` in `bf.yaml`, then run it three ways:

| Run it…     | With                                                 | For example                                |
| ----------- | ---------------------------------------------------- | ------------------------------------------ |
| Now         | `bf run ROUTINE [ARGS]...`                           | Prepare this week's review.                |
| On an event | `bf run --hook EVENT`                                | Validate the brain before each Git commit. |
| When due    | `refresh` with `bf update`, `bf watch` or a schedule | A review every seven days.                 |

A routine's `output` decides what its stdout becomes. `log`, the default, keeps it in the routine's [log](limits.md#processes-and-logs), `logs/NAME.log`. `action` turns nonempty Markdown into a dated action for you or your agent to work through.

## Run routines from hooks

A Git pre-commit hook can refuse a commit while the brain has validation problems. The brain must be a Git repository: if it is not yet, run `git init` inside it first. The routine is `bf validate` itself. Add it to `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  validate:
    command: [bf, validate]
    hooks: [pre-commit]
```

Then create the executable file `.git/hooks/pre-commit` in the brain:

```sh
#!/bin/sh
exec bf run --hook pre-commit
```

`bf run --hook pre-commit` runs every enabled routine listing that hook, in name order. On a valid brain, Git shows `{"dry_run":false,"hook":"pre-commit","ok":true,"routines":[{"routine":"validate","status":"ran"}]}` and commits. When a note links to a missing file, the routine reports `"status":"failed"`, `bf run` exits 1 and Git refuses the commit. The validation reply, naming the file to repair, is in `logs/validate.log`. The [routine examples](https://github.com/fmind/brain-framework/tree/main/examples/routines#validate-before-each-commit) run this walkthrough in a disposable Git repository.

A hook that no routine lists runs nothing, reads no input and succeeds, so the Git hook can exist before its routines. `bf validate` checks the working tree, including unstaged edits. For `pre-push`, write `exec bf run --hook pre-push -- "$@"`: Git's arguments and the ref lines it pipes reach each routine.

The hook, like a routine's nested `bf` call, runs the first `bf` on `PATH`. In a brain that [pins its runtime](upgrades.md#pin-a-brains-runtime), write `exec uv run --locked bf run --hook pre-commit`: `uv` also puts the pinned `bf` first on its routines' `PATH`.

## Prepare a weekly review

The reviewed [weekly review](https://github.com/fmind/brain-framework/blob/main/examples/routines/weekly-review.py) reads your projects, recent activity and program health and prints an action with a review list. Copy it from the release tag matching `bf --version`, make it executable, since a download loses that bit, then review it:

```bash
mkdir -p routines
curl -fsSLo routines/weekly-review.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/routines/weekly-review.py"
chmod +x routines/weekly-review.py
```

Add it under `routines:` in `bf.yaml`; like the example sensors, it needs Python 3.11 or later as `python3`, or uv in front of its command, as [sensor settings](sensors.md#sensor-settings) shows:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
```

```bash
bf run weekly-review
bf read actions
bf validate
```

When projects need review, tasks are open, items fall in the last seven days or scheduled programs failed, are `overdue`, `never` succeeded or [fell silent](schedule.md#silent-sources), the reply names the new `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`, with a random 8-character suffix. The action lists those programs first, then projects as plain bullets, overdue reviews first and earliest deadline first, then open-task counts, up to ten open tasks with their sections, the last seven days, with records by source, changed notes and recent actions, and the coming week. A list cut at its preview names the `bf read` page that holds the rest. Those programs leave out the review's own run, which BF records only after the routine exits: the review recognizes it by its file name, so keep the routine named `weekly-review`, and by its brain's name, so the `weekly-review` of a brain declared under `brains:` stays listed. While a sensor needs attention, an empty period or coming week says that it may have missed evidence. The list creates no duplicate checkbox tasks; complete work in the owning notes. Empty output creates nothing. With `refresh: 604800`, `bf update` and `bf watch` also run it weekly.

An action routine writes at most one action per day in a brain: a second run that day reports `"status":"skipped"` and keeps the existing action, even one another clone wrote. Only a folder holding an `ACTION.md` counts as today's action, not one left with a killed write's temporary file or an editor's lock. BF validates the Markdown's OKF metadata, declared relations and relative links, including headings, before writing; invalid or excessive output, or a failure, creates nothing. `bf validate` also checks its `bf://` targets and cited records.

Preview with `bf run weekly-review --dry-run`: it runs the routine but writes no action. The reply holds the Markdown as `text` and names no action folder, since the real run draws its own suffix; when today's action exists, it reports `"status":"skipped"` as the real run would.

## Routine settings

| Setting     | Default  | Meaning                                                                                               |
| ----------- | -------- | ----------------------------------------------------------------------------------------------------- |
| `command`   | required | A program on PATH or in `routines/`, then its arguments.                                              |
| `output`    | `log`    | `log` keeps stdout in `logs/NAME.log`; `action` writes nonempty Markdown as an action.                |
| `hooks`     | `[]`     | Events, such as `pre-commit`, that `bf run --hook EVENT` runs.                                        |
| `enabled`   | `true`   | A disabled routine never runs: `bf run` names it and runs nothing.                                    |
| `refresh`   | `0`      | Seconds between due runs; `0` leaves the routine out of updates.                                      |
| `lookback`  | `86400`  | Seconds covered by the first run.                                                                     |
| `timeout`   | `300`    | Maximum runtime in seconds.                                                                           |
| `max_bytes` | 1 MiB    | Maximum stdout of an `action` routine; a `log` routine's stdout only feeds its log.                   |
| `keep`      | none     | An `action` routine keeps its newest `keep` actions; see [keep recent reports](#keep-recent-reports). |

[Limits](limits.md#size-bounds) gives each setting's range, and [program names](schema.md#program-names) the naming rules: a routine's name also names the action folders it writes. Arguments support `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}`. `start` is the end of the last reviewed window, or `lookback` before the first run; `end` is now.

## Keep recent reports

A report routine writes one action a day or a week, and its old ones crowd `bf read actions`. Set `keep` to the number of newest actions to retain:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
    keep: 2
```

After writing a new action, the routine removes its older actions beyond the newest two, but only folders whose only entry is the unchanged `ACTION.md` it wrote on this machine: an action you edited, one holding other files, and one written before you set `keep` or by another clone stay. The new action always counts as the newest, even when another clone, in a time zone ahead, dated one later. Whatever BF cannot read or remove stays, and `logs/NAME.log` names it: a link or unreadable folder anywhere in `actions/` keeps every older action. The run's reply counts the folders it removed as `removed`. Remove older actions by hand once when you first set `keep`.

## Run a routine now

`bf run ROUTINE ARGS...` appends the arguments to the routine's command; put `--` before arguments that start with a dash. `--stdin` passes piped input to the routine, within the [input limit](limits.md#size-bounds):

```bash
echo "release notes" | bf run summarize --stdin -- --verbose
```

Without `--stdin`, a direct run passes no input, so a shell that holds its standard input open, as some agent hosts do, never delays it. `bf run --hook EVENT` passes what Git pipes, such as the refs of a push, to the routines listing the hook, and reads nothing when none does.

The reply lists each routine with its `status`: `ran`, `skipped` or `failed` with an `error` naming its log. One failure never stops the other routines of a hook. `bf run` acts on one brain, like `bf update`, and [`--dry-run`](commands.md#preview-an-update-or-a-collection) runs the routines without writing an action or recording the run.

## Write a routine

Read the brain with `bf read` and `bf search` using literal arguments. Programs receive `BF_BRAIN` set to the brain running them, so a nested `bf` call reads that brain whatever your shell selected. Keep routines offline and deterministic; do not edit notes or call a model: an [assistant](prompts.md) runs your agent. Name each item by the `uri` it returns, or by its `ref` when it has none, so a reviewer chooses what to open; a link from a dated action would count as [newer evidence](brain.md#review-reminders) for its target.

An `output: action` routine prints one complete OKF note with a nonempty `type`, such as:

```markdown
---
type: action
status: draft
summary: Review the New website project before the next work session.
---

# New website review

## Tasks

- [ ] Check whether the product page now explains the product before signup.
```

Relative links resolve from the new `ACTION.md`, two folders below the brain root. Test the script with a fake `bf`; it runs with the same [process safeguards](limits.md#processes-and-logs) as a sensor. A failed routine retries after the [failure backoff](schedule.md#timing-and-health), and `bf status` reports its health and latest `action` among the [status fields](commands.md#status-sources-and-routines).
