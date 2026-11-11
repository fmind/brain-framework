---
icon: lucide/repeat
description: Run deterministic brain programs now, on a schedule or from Git hooks, and turn their output into logs or review actions.
---

# Run routines

A routine is a deterministic program your brain runs: a validation gate, a digest or a weekly review. It uses no model; to run an agent harness, declare an [assistant](prompts.md). Declare routines under `routines:` in `bf.yaml`:

| Run it…     | With                                                      | Example                                       |
| ----------- | --------------------------------------------------------- | --------------------------------------------- |
| Now         | `bf run ROUTINE [ARGS]...`                                | Prepare this week's review.                   |
| On an event | `bf run --hook EVENT`                                     | Validate the brain before each Git commit.    |
| When due    | `refresh` or `at` with `bf update`, `bf watch` or a timer | Run a review weekly or each weekday at 08:00. |

`output: log` (default) appends stdout to `logs/NAME.log`. `output: action` turns nonempty OKF Markdown into a dated `status: draft` action folder under `actions/`.

## Run routines from hooks

Refuse Git commits while the brain has validation problems by declaring `bf validate` under `routines:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  validate:
    command: [bf, validate]
    hooks: [pre-commit]
```

Create `.git/hooks/pre-commit` (`chmod +x`):

```sh
#!/bin/sh
exec bf run --hook pre-commit
```

`bf run --hook pre-commit` runs every enabled routine listing that hook in name order (`{"dry_run":false,"hook":"pre-commit","ok":true,"routines":[{"routine":"validate","status":"ran"}]}`). Any failure exits 1 after running all matching routines and writes details to `logs/NAME.log`.

- A hook with no matching routines reads no stdin and exits 0.
- `output: log` routines never wait on brain write locks; `output: action` waits up to 120 seconds.
- For `pre-push`, use `exec bf run --hook pre-push -- "$@"` to forward Git's arguments and ref lines on stdin. In a brain that [pins its runtime](upgrades.md#pin-a-brains-runtime), prefix with `uv run --locked`.

## Prepare a weekly review

The reviewed [weekly review](https://github.com/fmind/brain-framework/blob/main/examples/routines/weekly-review.py) uses [`_bf.py`](https://github.com/fmind/brain-framework/blob/main/examples/routines/_bf.py) to read projects, recent activity and health, then emits a single review action. Copy both files from the release tag matching `bf --version`:

```bash
mkdir -p routines
examples="https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/routines"
curl -fsSLo routines/_bf.py "$examples/_bf.py"
curl -fsSLo routines/weekly-review.py "$examples/weekly-review.py"
chmod +x routines/weekly-review.py
```

Add `weekly-review` under `routines:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
    keep: 2
```

```bash
bf run weekly-review
bf read actions
bf validate
```

When projects need review, tasks are open, items arrived in the last seven days or scheduled programs need attention, the routine writes `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`; empty output creates nothing. Keep the routine named `weekly-review` so it excludes its own in-progress run from program health. Preview with `bf run weekly-review --dry-run`.

## Routine settings

| Setting     | Default  | Meaning                                                                                                                                                      |
| ----------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `command`   | required | Program on PATH or in `routines/`, `sensors/` or `channels/`, plus arguments; optional only with `enabled: false`.                                           |
| `output`    | `log`    | `log` appends stdout to `logs/NAME.log`; `action` writes nonempty OKF Markdown as an action.                                                                 |
| `hooks`     | `[]`     | Hook names (such as `pre-commit`) run by `bf run --hook EVENT`.                                                                                              |
| `enabled`   | `true`   | Disabled routines never run.                                                                                                                                 |
| `refresh`   | `0`      | Seconds between due runs (`0` = manual unless `at` is set); mutually exclusive with `at`.                                                                    |
| `at`        | none     | Wall-clock `"HH:MM"` run once per listed day; mutually exclusive with nonzero `refresh`.                                                                     |
| `days`      | all days | Subset of `[mon, tue, wed, thu, fri, sat, sun]` when `refresh` or `at` may run.                                                                              |
| `timezone`  | local    | IANA time zone (such as `Europe/Paris`) used to evaluate `at` and `days`.                                                                                    |
| `when`      | none     | Declarative gate `{sources?, review?, actions?, attention?}` evaluated offline before starting; skips as `gated` when unmatched ([gates](prompts.md#gates)). |
| `lookback`  | `86400`  | Seconds covered by the first run's `{{start}}`..`{{end}}` window.                                                                                            |
| `timeout`   | `300`    | Maximum runtime in seconds.                                                                                                                                  |
| `max_bytes` | 1 MiB    | Maximum stdout bytes for `output: action`.                                                                                                                   |
| `keep`      | none     | Retain at most the newest `keep` untouched actions written by this routine on this machine.                                                                  |

Arguments support `{{brain}}`, `{{home}}`, `{{start}}`, `{{end}}` and `{{lines:FILE}}`, and receive `BF_BRAIN`, `BF_PROGRAM`, `BF_START`, `BF_END` and `BF_STATE_DIR` ([sensor settings](sensors.md#what-a-program-receives)). Direct runs (`bf run ROUTINE [--stdin] [-- ARGS...]`) append extra arguments and pass stdin only when `--stdin` is set.

## Keep recent reports

With `keep: 2`, writing a new action prunes older folders of that routine beyond the newest two (`removed` in the run reply). Pruning deletes only folders whose sole entry is the unedited `ACTION.md` written on this machine — edited actions, extra files and actions written before `keep` or on another clone stay intact.

## Write a routine

Copy [`examples/routines/_bf.py`](https://github.com/fmind/brain-framework/blob/main/examples/routines/_bf.py) beside your scripts for standard-library helpers:

- `window()`, `state_dir()`, `load_state(name, default=None)` and `save_state(name, data)` manage windows and atomic JSON state in `BF_STATE_DIR`.
- `bf_json(*args)`, `bf_pages(ref="", ...)` and `bf_export(scope="", *, kind="items", brain=..., observed_since=...)` call `bf` safely and check exit codes and incomplete flags.
- `visible(text)`, `plain(value)`, `code(value)`, `emit_events(events)` and `emit_action(frontmatter, body)` sanitize untrusted titles and format bounded OKF actions or gate events.

Cite items in action reports by `uri` or plain text `ref` rather than Markdown links so a generated report does not count as [newer evidence](brain.md#review-reminders) on linked projects. An `output: action` script prints one OKF note:

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

## Action routine rules

- **One action a day**: an `output: action` routine writes at most one action folder containing `ACTION.md` per local date, reporting `"status":"skipped"` on later runs that day.
- **Validated before writing**: BF checks OKF frontmatter, declared relations and relative links (resolved from `actions/YYYY-MM-DD_NAME-SUFFIX/ACTION.md`) before saving; invalid output creates nothing.
