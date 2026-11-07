---
icon: lucide/repeat
description: Run deterministic brain programs now, on a schedule or from Git hooks, and turn their output into logs or review actions.
---

# Run routines

A routine is any deterministic program your brain runs: a validation, a backup, a weekly review. It uses no model and makes no decisions for you; to run your agent harness, use an [assistant](prompts.md) instead. Declare it under `routines:` in `bf.yaml`, then run it three ways:

| Run it…     | With                                                 | For example                                |
| ----------- | ---------------------------------------------------- | ------------------------------------------ |
| Now         | `bf run ROUTINE [ARGS]...`                           | Prepare this week's review.                |
| On an event | `bf run --hook EVENT`                                | Validate the brain before each Git commit. |
| When due    | `refresh` with `bf update`, `bf watch` or a schedule | A review every seven days.                 |

A routine's `output` decides what its stdout becomes:

- `log`, the default, keeps it in the routine's [log](limits.md#processes-and-logs), `logs/NAME.log`.
- `action` turns nonempty Markdown into a dated action for you or your agent to work through.

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

`bf run --hook pre-commit` runs every enabled routine listing that hook, in name order. On a valid brain, Git shows `{"dry_run":false,"hook":"pre-commit","ok":true,"routines":[{"routine":"validate","status":"ran"}]}` and commits. When a note links to a missing file, the routine reports `"status":"failed"`, `bf run` exits 1 and Git refuses the commit. `logs/validate.log` holds the validation reply, which names the file to repair. The [routine examples](https://github.com/fmind/brain-framework/tree/main/examples/routines#validate-before-each-commit) run this walkthrough in a disposable Git repository.

- A hook that no routine lists runs nothing, reads no input and succeeds, so the Git hook can exist before its routines.
- `bf validate` checks the working tree, including unstaged edits.
- For `pre-push`, write `exec bf run --hook pre-push -- "$@"`: Git's arguments and the ref lines it pipes reach each routine.
- The hook, like a routine's nested `bf` call, runs the first `bf` on `PATH`. In a brain that [pins its runtime](upgrades.md#pin-a-brains-runtime), write `exec uv run --locked bf run --hook pre-commit`: `uv` also puts the pinned `bf` first on its routines' `PATH`.

## Prepare a weekly review

The reviewed [weekly review](https://github.com/fmind/brain-framework/blob/main/examples/routines/weekly-review.py) reads your projects, recent activity and program health, then prints an action with a review list. Copy it from the release tag matching `bf --version` and make it executable, since a download loses that bit. Then review it:

```bash
mkdir -p routines
curl -fsSLo routines/weekly-review.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/routines/weekly-review.py"
chmod +x routines/weekly-review.py
```

Add it under `routines:` in `bf.yaml`. Like the example sensors, it needs Python 3.11 or later as `python3`, or uv in front of its command, as [sensor settings](sensors.md#sensor-settings) shows:

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

The review writes an action when projects need review, tasks are open, items fall in the last seven days, or scheduled programs failed, are `overdue`, `never` succeeded or [fell silent](schedule.md#silent-sources). The reply names the new `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`, with a random 8-character suffix. Empty output creates nothing. With `refresh: 604800`, `bf update` and `bf watch` also run it weekly.

The action lists, in order:

1. Those programs.
2. Projects as plain bullets, overdue reviews first, then earliest deadline first.
3. Open-task counts and up to ten open tasks with their sections.
4. The last seven days: records by source, changed notes and recent actions.
5. The coming week.

A list cut at its preview names the `bf read` page that holds the rest. While a sensor needs attention, an empty period or coming week says that it may have missed evidence. The review creates no duplicate checkbox tasks: complete work in the owning notes.

Keep the routine named `weekly-review`. BF records the review's own run only after the routine exits, so the review leaves that run out of its program list. It recognizes the run by its routine name, which must match the script's file name, and by its brain's name. The `weekly-review` of a brain declared under `brains:` therefore stays listed.

Preview with `bf run weekly-review --dry-run`: it runs the routine but writes no action. The reply holds the Markdown as `text` and names no action folder, since the real run draws its own suffix. When today's action exists, it reports `"status":"skipped"` as the real run would. [Action routine rules](#action-routine-rules) give the daily limit and the checks BF runs before writing.

## Routine settings

| Setting     | Default  | Meaning                                                                                                                  |
| ----------- | -------- | ------------------------------------------------------------------------------------------------------------------------ |
| `command`   | required | A program on PATH or in `routines/`, `sensors/` or `channels/`, then its arguments; optional only with `enabled: false`. |
| `output`    | `log`    | `log` keeps stdout in `logs/NAME.log`; `action` writes nonempty Markdown as an action.                                   |
| `hooks`     | `[]`     | Events, such as `pre-commit`, that `bf run --hook EVENT` runs.                                                           |
| `enabled`   | `true`   | A disabled routine never runs: `bf run` names it and runs nothing.                                                       |
| `refresh`   | `0`      | Seconds between due runs; `0` leaves the routine out of updates.                                                         |
| `lookback`  | `86400`  | Seconds covered by the first run.                                                                                        |
| `timeout`   | `300`    | Maximum runtime in seconds.                                                                                              |
| `max_bytes` | 1 MiB    | Maximum stdout of an `action` routine; a `log` routine's stdout only feeds its log.                                      |
| `keep`      | none     | An `action` routine keeps its newest `keep` actions; see [keep recent reports](#keep-recent-reports).                    |

- [Limits](limits.md#size-bounds) gives each setting's range.
- [Program names](schema.md#program-names) gives the naming rules. A routine's name also names the action folders it writes.
- Arguments support `{{brain}}`, `{{home}}`, `{{start}}`, `{{end}}` and `{{lines:FILE}}`, as [sensor settings](sensors.md#sensor-settings) describe. `start` is the end of the last reviewed window, or `lookback` before the first run; `end` is now.
- A routine also receives its window and a private state directory as [environment variables](sensors.md#what-a-program-receives), like a sensor.

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

After writing a new action, the routine removes its older actions beyond the newest two. The run's reply counts the folders it removed as `removed`. Remove older actions by hand once when you first set `keep`, because pruning removes only folders whose only entry is the unchanged `ACTION.md` it wrote on this machine. These stay:

- an action you edited;
- an action holding other files;
- an action written before you set `keep`, or by another clone;
- whatever BF cannot read or remove, which `logs/NAME.log` names. A link or unreadable folder anywhere in `actions/` keeps every older action.

The new action always counts as the newest, even when another clone in a time zone ahead dated its own action a day later.

## Run a routine now

`bf run ROUTINE ARGS...` appends the arguments to the routine's command; put `--` before arguments that start with a dash. `--stdin` passes piped input to the routine, within the [input limit](limits.md#size-bounds):

```bash
echo "release notes" | bf run summarize --stdin -- --verbose
```

Without `--stdin`, a direct run passes no input, so a shell that holds its standard input open, as some agent hosts do, never delays it. `bf run --hook EVENT` passes what Git pipes, such as the refs of a push, to the routines listing the hook, and reads nothing when none does.

The reply lists each routine with its `status`: `ran`, `skipped`, or `failed` with an `error` naming its log. One failure never stops the other routines of a hook. `bf run` acts on one brain, like `bf update`. [`--dry-run`](commands.md#preview-an-update-or-a-collection) runs the routines without writing an action or recording the run.

## Write a routine

Read the brain with `bf read` and `bf search`, using literal arguments. Programs receive `BF_BRAIN` set to the brain running them, so a nested `bf` call reads that brain whatever your shell selected. Keep routines offline and deterministic: do not edit notes or call a model, since an [assistant](prompts.md) runs your agent.

Name each item by the `uri` it returns, or by its `ref` when it has none, so a reviewer chooses what to open. Do not link it: a link from a dated action would count as [newer evidence](brain.md#review-reminders) for its target.

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

Relative links resolve from the new `ACTION.md`, two folders below the brain root. Test the script with a fake `bf`; it runs with the same [process safeguards](limits.md#processes-and-logs) as a sensor. A failed routine retries after the [failure backoff](schedule.md#timing-and-health). `bf status` reports its health and latest `action` among the [status fields](commands.md#status-sources-and-routines).

## Action routine rules

- **One action a day**: an action routine writes at most one action per day in a brain. A second run that day reports `"status":"skipped"` and keeps the existing action, even one another clone wrote.
- **What counts as today's action**: only a folder holding an `ACTION.md`. A folder left with a killed write's temporary file or an editor's lock does not count.
- **Checks before writing**: BF validates the Markdown's OKF metadata, declared relations and relative links, including headings. Invalid or excessive output, or a failure, creates nothing.
- **Checks after writing**: `bf validate` also checks the action's `bf://` targets and cited records.
