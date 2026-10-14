# Example routines

Standalone, deterministic brain programs. Copy the ones you need into a brain, declare them under `routines:` in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Routine            | Configuration                            | Result                                                                                                                                                      |
| ------------------ | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `weekly-review.py` | `output: action`, weekly `refresh`       | A draft action listing programs needing attention, projects due for review, a counted open-task preview, the last seven days by source and the coming week. |
| `bf validate`      | `hooks: [pre-commit]`, no script to copy | A Git commit stops while the brain has validation problems; the reply stays in `logs/validate.log`.                                                         |

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

The dry run lists the due sensor and routine. The update collects one fictional record, then reports the routine with `"status":"ran"` and an `action` at `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`, where `XXXXXXXX` is a random suffix. The action lists projects as plain bullets, overdue reviews first and earliest deadline first, plus open-task counts and source refs; validation returns `"valid":true`. A second update within the week creates nothing. Read the returned action to continue the review: the routine completes no task for you.

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

[Prepare a weekly review](../../docs/docs/routines.md#prepare-a-weekly-review) copies the routine into your brain, configures it and previews its action. The routine only reads pages: it never edits notes and links nothing, naming items by their `uri` or ref, because a link from a dated action would count as newer evidence for its target. An incomplete page makes it write nothing, and BF retries after the [failure backoff](../../docs/docs/schedule.md#timing-and-health).
