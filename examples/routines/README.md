# Example routines

Standalone, deterministic brain programs. Copy the ones you need into a brain, declare them under `routines:` in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Routine            | Configuration                            | Result                                                                                                                          |
| ------------------ | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `weekly-review.py` | `output: action`, weekly `refresh`       | A draft action listing projects due for review, a counted open-task preview, the last seven days by source and the coming week. |
| `bf validate`      | `hooks: [pre-commit]`, no script to copy | A Git commit stops while the brain has validation problems; the reply stays in `logs/validate.log`.                             |

Both walkthroughs run from the framework checkout after `uv sync --locked`, in a subshell that isolates configuration and state and removes its temporary files on exit.

## Prepare a weekly review

This review uses the fictional example brain and runs only its local demo sensor and the routine:

```bash
(
  set -eu
  bf_checkout=$PWD
  routine_demo=$(mktemp -d)
  routine_demo=$(cd "$routine_demo" && pwd -P)
  trap 'rm -rf -- "$routine_demo"' EXIT
  unset BF_BRAIN
  export XDG_CONFIG_HOME="$routine_demo/config" XDG_STATE_HOME="$routine_demo/state" TZ=UTC
  cp -R examples/brain "$routine_demo/brain"
  mkdir "$routine_demo/brain/routines"
  cp examples/routines/weekly-review.py "$routine_demo/brain/routines/"
  cd "$routine_demo/brain"
  cat >> bf.yaml <<'EOF'
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
EOF
  bf() { uv run --project "$bf_checkout" bf "$@"; }
  bf update --dry-run
  bf update
  bf read actions
  bf search "Weekly review" --scope actions
  bf validate
)
```

The dry run lists the due sensor and routine. The update collects one fictional record, then reports the routine with `"status":"ran"` and an `action` at `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`, where `XXXXXXXX` is a random suffix. The action lists open-task counts and source refs; validation returns `"valid":true`. A second update within the week creates nothing. Read the returned action to continue the review: the routine completes no task for you.

## Validate before each commit

This routine is `bf validate` itself, run by a Git pre-commit hook. The hook calls `bf` from PATH, so the subshell puts the checkout's `bf` first:

```bash
(
  set -eu
  hook_demo=$(mktemp -d)
  hook_demo=$(cd "$hook_demo" && pwd -P)
  trap 'rm -rf -- "$hook_demo"' EXIT
  unset BF_BRAIN
  export PATH="$PWD/.venv/bin:$PATH" XDG_CONFIG_HOME="$hook_demo/config" XDG_STATE_HOME="$hook_demo/state"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  bf init "$hook_demo/brain"
  cd "$hook_demo/brain"
  git init --quiet --initial-branch=main
  git config user.name Example
  git config user.email example@example.invalid
  cat >> bf.yaml <<'EOF'
routines:
  validate:
    command: [bf, validate]
    hooks: [pre-commit]
EOF
  printf '#!/bin/sh\nexec bf run --hook pre-commit\n' > .git/hooks/pre-commit
  chmod +x .git/hooks/pre-commit
  git add --all
  git commit --quiet --message "Create the brain" && echo "The first commit passed."
  printf -- '---\ntype: project\nstatus: draft\n---\n\n# Launch\n\nSee [the plan](plan.md).\n' > projects/launch.md
  git add projects/launch.md
  git commit --quiet --message "Add the launch project" || echo "The hook refused the second commit."
  bf run validate || tail -n 2 logs/validate.log
)
```

Git shows the hook's reply. The first commit passes with `{"dry_run":false,"hook":"pre-commit","ok":true,"routines":[{"routine":"validate","status":"ran"}]}`. The second note links to a missing `plan.md`, so the hook reports `"status":"failed"`, exits 1 and Git refuses the commit. `bf run validate` fails the same way; the log's last entries hold the validation reply, with the problem `{"error":"broken link: plan.md","file":"projects/launch.md"}`, then the failure line.

`bf validate` checks the working tree, including unstaged edits, not only the staged snapshot. A routine with `output: log`, the default, keeps its output in `logs/NAME.log`; `bf init` ignores `logs/` in Git. For `pre-push` hooks and pinned brains, see [Run routines from hooks](../../docs/docs/routines.md#run-routines-from-hooks).

## Use the weekly review in your brain

From the brain folder, copy the routine from the release tag matching `bf --version` and make it executable: the configuration below runs it as a command, and a download does not keep the executable bit.

```bash
mkdir -p routines
curl -fsSLo routines/weekly-review.py "https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/routines/weekly-review.py"
chmod +x routines/weekly-review.py
```

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
```

Preview the review before creating an action. Run from the brain folder with Python 3.11 or later as `python3`, replacing the timestamp with the review time:

```bash
routines/weekly-review.py "$PWD" 2026-09-27T12:00:00Z
```

Output starting with OKF metadata (`type: action`, `status: draft`) is the review; empty output means there is nothing to review. Its Coming week lists each item by its `date`, or by the local time `bf read` returns, with its offset, such as `2026-09-26 11:00+02:00`. The timestamp sets the note's date; page reads use the current brain. `bf run weekly-review` creates the action now, and `bf update` creates it when due. A second run on the same day reports `"status":"skipped"` and keeps the existing action; `bf run weekly-review --dry-run` returns the review as `text` without writing it, but still runs the routine.

The routine reads the brain BF passes as `{{brain}}` with the first `bf` on PATH; a scheduled run uses the PATH that `bf schedule` captured. In a brain that [pins its runtime](../../docs/docs/upgrades.md#pin-a-brains-runtime), run `bf update`, `bf watch` and `bf schedule` through the pin so that this `bf` is the pinned release.

## Contract

`tests/test_adapters_routines.py` checks every script with a fake `bf` executable.

- Python 3.11+ standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Deterministic: the same pages produce the same Markdown. No model, network or provider call; read the brain through `bf read` and `bf search` with literal arguments.
- An `output: action` routine prints one OKF action or nothing. BF validates its metadata and declared links before writing. Use `status: draft|stable|deprecated` for note maturity and checkboxes for work progress.
- Never link from the action: a link from a dated action counts as newer evidence for its target, so every later review would list the earlier ones. Name notes and records by their returned `uri`, or by ref when items omit it: two selected brains can hold the same ref. Do not copy record titles into authored notes.
- A failed or incomplete page (`problems`, `stale`) exits nonzero with nothing on stdout and one generic sentence on stderr; BF then writes nothing and retries after the [failure backoff](../../docs/docs/sensors.md#collect-and-update).
- Project review follows every `next_offset`, up to 100 pages, and fails before writing when a continuation stalls or more pages remain.
- Task counts cover the selected notes; up to ten open tasks are previewed with source refs and lines, as plain bullets that duplicate no checkbox. Follow `bf read tasks` for the complete list.
- A routine never edits notes: people and agents read its action and update the owning notes.
