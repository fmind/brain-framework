---
description: Run deterministic brain programs now, on a schedule or from Git hooks, and turn their output into logs or review actions.
---

# Run routines

A routine is any deterministic program your brain runs: a validation, a backup, a weekly review. It uses no model and makes no decisions for you. Declare it under `routines:` in `bf.yaml`, then run it three ways:

| Run it…     | With                                                 | For example                                |
| ----------- | ---------------------------------------------------- | ------------------------------------------ |
| Now         | `bf run ROUTINE [ARGS]...`                           | Prepare this week's review.                |
| On an event | `bf run --hook EVENT`                                | Validate the brain before each Git commit. |
| When due    | `refresh` with `bf update`, `bf watch` or a schedule | A review every seven days.                 |

A routine's `output` decides what its stdout becomes. `log`, the default, keeps it in `logs/NAME.log`. `action` turns nonempty Markdown into a dated action for you or your agent to work through.

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

A hook that no routine lists runs nothing and succeeds, so the Git hook can exist before its routines. `bf validate` checks the working tree, including unstaged edits. For `pre-push`, write `exec bf run --hook pre-push "$@"`: Git's arguments and the ref lines it pipes reach each routine.

## Prepare a weekly review

The reviewed [weekly review](https://github.com/fmind/brain-framework/blob/main/examples/routines/weekly-review.py) reads your projects and recent activity and prints an action with review tasks. Copy it from the release tag matching `bf --version`, then review it:

```bash
mkdir -p routines
curl -fsSLo routines/weekly-review.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/routines/weekly-review.py"
```

Add it under `routines:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [uv, run, --no-project, --python, "3.14", routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
```

```bash
bf run weekly-review
bf read actions
bf validate
```

When projects need review, tasks are open or items are dated in the last seven days, the reply names the new `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`, with a random 8-character suffix. The action lists open-task counts, up to ten open tasks with their sections, and recent activity by source. Empty output creates nothing. With `refresh: 604800`, `bf update` and `bf watch` also run it weekly.

An action routine writes at most one action per day in a brain: a second run that day reports `"status":"skipped"` and keeps the existing action, even one another clone wrote. BF validates the Markdown's OKF metadata and links before writing; invalid or excessive output, or a failure, creates nothing. Preview without writing with `bf run weekly-review --dry-run`: the reply includes the Markdown as `text`.

## Routine settings

| Setting     | Default  | Meaning                                                                                |
| ----------- | -------- | -------------------------------------------------------------------------------------- |
| `command`   | required | A program on PATH or in `routines/`, then up to 127 arguments.                         |
| `output`    | `log`    | `log` keeps stdout in `logs/NAME.log`; `action` writes nonempty Markdown as an action. |
| `hooks`     | `[]`     | Up to 16 events, such as `pre-commit`, that `bf run --hook EVENT` runs.                |
| `enabled`   | `true`   | A disabled routine never runs.                                                         |
| `refresh`   | `0`      | Seconds between due runs, up to 365 days; zero leaves it out of updates.               |
| `lookback`  | `86400`  | Seconds covered by the first run.                                                      |
| `timeout`   | `300`    | Maximum runtime in seconds, from 1 to 3,600.                                           |
| `max_bytes` | 1 MiB    | Maximum stdout; configurable up to 4 MiB.                                              |

Names start with a lowercase letter and use lowercase letters and digits joined by single hyphens; sensor and routine names must differ. Arguments support `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}`. `start` is the end of the last reviewed window, or `lookback` before the first run; `end` is now.

## Run a routine now

`bf run ROUTINE ARGS...` appends the arguments to the routine's command; put `--` before arguments that start with a dash. Piped input reaches the routine, up to 1 MiB, and a pipe must close within 10 seconds:

```bash
echo "release notes" | bf run summarize -- --verbose
```

The reply lists each routine with its `status`: `ran`, `skipped` or `failed` with an `error` naming its log. One failure never stops the other routines of a hook. `--dry-run` runs the routines but writes no action and records no run; logs still grow. `bf run` acts on one brain, like `bf update`.

## Logs

Each sensor and routine appends to `logs/NAME.log` in the brain, newest entry last, and keeps the latest 1 MiB. An entry starts with a `== TIME OUTCOME ==` line, followed by a log routine's stdout and any stderr. Status and errors name these brain-relative paths. `bf init` ignores `/logs/` in Git, and search never reads logs. Logs can hold provider output: keep them private and never paste them into a public issue.

## Write a routine

Read the brain with `bf read` and `bf search` using literal arguments. Programs receive `BF_BRAIN` set to the brain running them, so a nested `bf` call reads that brain whatever your shell selected. Keep routines offline and deterministic; do not edit notes or call a model. Name collected evidence by ref so a reviewer chooses what to open.

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

Relative links resolve from the new `ACTION.md`, two folders below the brain root. Test the script with a fake `bf`; it runs with the same [process safeguards](limits.md#processes-and-logs) as a sensor. A failed routine retries with the sensors' [failure backoff](sensors.md#collect-and-update). `bf status` reports each routine's `state`, `freshness`, `last_success`, latest `action`, and `failed`, `error`, `failures` and `log` after a failure; see [status fields](commands.md#status-sources-and-routines).
