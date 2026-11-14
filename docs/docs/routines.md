---
icon: lucide/repeat
description: Run deterministic brain programs now, on a schedule or from Git hooks, and turn their output into logs or review actions.
---

# Run routines

A routine is a deterministic job your brain runs (`run:` under `jobs:` in `bf.yaml`): a validation gate, a digest or a weekly review. It uses no model; to run an agent harness, add `prompt:` on a [job](prompts.md). Declare deterministic jobs under `jobs:` in `bf.yaml`:

| Run it…     | With                                                          | Example                                       |
| ----------- | ------------------------------------------------------------- | --------------------------------------------- |
| Now         | `bf run JOB [ARGS]...`                                        | Prepare this week's review.                   |
| On an event | `bf run --hook EVENT`                                         | Validate the brain before each Git commit.    |
| When due    | `on: { every }` or `on: { at }` with `bf update` / `bf watch` | Run a review weekly or each weekday at 08:00. |

`emit: log` (default) appends stdout to `logs/NAME.log`. `emit: action` turns nonempty OKF Markdown into a dated `status: draft` action folder under `actions/`.

## Run routines from hooks

Refuse Git commits while the brain has validation problems by declaring `bf validate` under `jobs:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
jobs:
  validate:
    run: [bf, validate]
    on: { hook: pre-commit }
```

Create `.git/hooks/pre-commit` (`chmod +x`):

```sh
#!/bin/sh
exec bf run --hook pre-commit
```

`bf run --hook pre-commit` runs every enabled job listing `hook: pre-commit` in name order (`{"dry_run":false,"hook":"pre-commit","ok":true,"jobs":[{"job":"validate","status":"ok","run":"..."}]}`). Any failure exits 1 after running all matching jobs and writes details to `logs/NAME.log`.

- A hook with no matching jobs reads no stdin and exits 0.
- `emit: log` jobs never wait on brain write locks; `emit: action` waits up to 120 seconds.
- For `pre-push`, use `exec bf run --hook pre-push -- "$@"` to forward Git's arguments and ref lines on stdin. In a brain that [pins its runtime](upgrades.md#pin-a-brains-runtime), prefix with `uv run --locked`.

## Prepare a weekly review

The standalone [weekly review](https://github.com/fmind/brain-framework/blob/main/examples/routines/weekly-review.py) reads projects, recent activity and health, then emits a single review action. Copy it from the release tag matching `bf --version`:

```bash
mkdir -p routines
examples="https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/routines"
curl -fsSLo routines/weekly-review.py "$examples/weekly-review.py"
chmod +x routines/weekly-review.py
```

Add `weekly-review` under `jobs:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
jobs:
  weekly-review:
    run: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    emit: action
    on: { every: 604800 }
    keep: 2
```

```bash
bf run weekly-review
bf read actions
bf validate
```

When projects need review, tasks are open, items arrived in the last seven days or scheduled jobs need attention, the script writes `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`; empty output creates nothing. Keep the job named `weekly-review` so it excludes its own in-progress run from job health. Preview with `bf run weekly-review --dry-run`.

## Routine settings

| Setting     | Default  | Meaning                                                                                                                           |
| ----------- | -------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `run`       | required | Program on `PATH` or in `routines/`, `sensors/` or `channels/`, plus arguments; optional only with `enabled: false`.              |
| `emit`      | `log`    | `log` appends stdout to `logs/NAME.log`; `action` writes nonempty OKF Markdown as an action.                                      |
| `on`        | none     | Single `{ every\|at\|hook\|event\|daemon }` trigger or list of triggers (`hook: pre-commit` runs via `bf run --hook pre-commit`). |
| `enabled`   | `true`   | Disabled jobs never run.                                                                                                          |
| `gate`      | none     | Pre-flight script (`exit 0` to proceed, nonzero to skip as `gated`); see [gates](prompts.md#gates).                               |
| `lookback`  | `86400`  | Seconds covered by the first run's `{{start}}`..`{{end}}` window.                                                                 |
| `timeout`   | `300`    | Maximum runtime in seconds (`1`–`3,600`).                                                                                         |
| `max_bytes` | 64 MiB   | Maximum stdout bytes.                                                                                                             |
| `keep`      | none     | Retain at most the newest `keep` untouched actions written by this job on this machine (`emit: action` or `emit: records`).       |

Arguments support `{{brain}}`, `{{home}}`, `{{state}}`, `{{run}}`, `{{start}}`, `{{end}}`, `{{cursor}}` and `{{lines:FILE}}`; subprocesses receive `BF_BRAIN` and variables listed in `requires.env` ([job inputs](sensors.md#what-a-program-receives)). Direct runs (`bf run JOB [--stdin] [-- ARGS...]`) append extra arguments to `run:` jobs and pass stdin only when `--stdin` is set.

## Keep recent reports

With `keep: 2`, writing a new action prunes older folders of that job beyond the newest two (`removed` in the run reply). Pruning deletes only folders whose sole entry is the unedited `ACTION.md` written on this machine — edited actions, extra files and actions written before `keep` or on another clone stay intact.

## Write a routine

Write self-contained standard-library Python scripts (see [`examples/routines/weekly-review.py`](https://github.com/fmind/brain-framework/blob/main/examples/routines/weekly-review.py) and [`records-gate.py`](https://github.com/fmind/brain-framework/blob/main/examples/routines/records-gate.py)):

- Accept `{{brain}}`, `{{start}}`, `{{end}}` and `{{state}}` as arguments instead of reading environment variables.
- Pass `--quiet` / JSON flags to `bf search`, `bf read` and `bf export --kind items`, check exit codes cleanly and sanitize untrusted titles before writing OKF notes.

Cite items in action reports by `uri` or plain text `ref` rather than Markdown links so a generated report does not count as [newer evidence](brain.md#review-reminders) on linked projects. An `emit: action` script prints one OKF note:

```markdown
---
type: action
status: draft
summary: Review the New website project before the next work session.
---

# New website review

## Tasks

- [ ] Check whether the product page explains the product before signup.
```

## Action job rules {#action-routine-rules}

- **Unique dated folders**: each `emit: action` run creates a distinct `actions/YYYY-MM-DD_JOB-SUFFIX/ACTION.md` folder (`status: draft`) unless stdout is empty or `NO_REPLY`.
- **Validated before writing**: BF checks OKF frontmatter, declared relations and relative links (resolved from `actions/YYYY-MM-DD_NAME-SUFFIX/ACTION.md`) before saving; invalid output creates nothing.
