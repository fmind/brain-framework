# Example routines

Standalone, deterministic brain programs. Copy the ones you need into a brain, declare them under `jobs:` in `bf.yaml` (directly or as a job's `gate:`), and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Routine            | Configuration                           | Result                                                                                                                                                  |
| ------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `weekly-review.py` | `emit: action`, `on: { every: 7d }`     | A draft action listing jobs needing attention, projects due for review, a counted open-task preview, the last seven days by source and the coming week. |
| `bf validate`      | `on: { hook: pre-commit }`              | A Git commit stops while the brain has validation problems; the output stays in `logs/validate.log`.                                                    |
| `records-gate.py`  | `jobs.<name>.gate` with `on: { every }` | Exits 0 and emits events when a source gained or changed matching records since the last check; exits 1 when none matched so the job skips as `gated`.  |

The weekly review and pre-commit walkthroughs run from the framework checkout after `uv sync --locked`, in a subshell that isolates configuration and state and removes its temporary files on exit.

## Prepare a weekly review

This review uses the fictional example brain and runs only its local demo collector and the review job:

```bash
(
  set -eu
  bf_checkout=$PWD
  routine_demo=$(mktemp -d)
  routine_demo=$(cd "$routine_demo" && pwd -P)
  trap 'rm -rf -- "$routine_demo"' EXIT
  unset BF_BRAIN
  export XDG_CONFIG_HOME="$routine_demo/config" TZ=UTC
  cp -R examples/brain "$routine_demo/brain"
  mkdir "$routine_demo/brain/routines"
  cp examples/routines/weekly-review.py "$routine_demo/brain/routines/"
  cd "$routine_demo/brain"
  python3 -c '
from pathlib import Path
text = Path("bf.yaml").read_text()
job = "\n  weekly-review:\n    run: [python3, routines/weekly-review.py, \"{{brain}}\", \"{{end}}\"]\n    on: { every: 7d }\n    emit: action\n"
Path("bf.yaml").write_text(text.replace("\nfields:\n", job + "fields:\n", 1))
'
  bf() { uv run --project "$bf_checkout" bf "$@"; }
  bf update --dry-run
  bf update
  bf read actions
  bf search "Weekly review" --scope actions
  bf validate
)
```

The dry run lists the due `demo` and `weekly-review` jobs. The update collects one fictional record, then saves an action at `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`, where `XXXXXXXX` is a random suffix. The action counts the seven open tasks across three notes and lists each with its section ref and line, then counts the period's records by source: `records by source: demo 1`. It says `No project note is due for review.`: the copied project is newer than the demo record it links, and its `stale_after` falls due on 2026-12-18, after which the action lists it with `review due 2026-12-18`. [Jobs](../../docs/docs/jobs.md) describes deterministic routines and scheduling. Validation returns `"valid":true`. A second update within the week creates nothing. Read the returned action to continue the review: the routine completes no task for you.

## Validate before each commit

This job runs `bf validate` from a Git pre-commit hook. The hook calls `bf` from `PATH`, so the subshell puts the checkout's `bf` first:

```bash
(
  set -eu
  hook_demo=$(mktemp -d)
  hook_demo=$(cd "$hook_demo" && pwd -P)
  trap 'rm -rf -- "$hook_demo"' EXIT
  unset BF_BRAIN
  export PATH="$PWD/.venv/bin:$PATH" XDG_CONFIG_HOME="$hook_demo/config"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  bf init "$hook_demo/brain"
  cd "$hook_demo/brain"
  git init --quiet --initial-branch=main
  git config user.name Example
  git config user.email example@example.invalid
  cat >> bf.yaml <<'EOF'
jobs:
  validate:
    run: [bf, validate]
    on: { hook: pre-commit }
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

Git shows the hook's reply. The first commit passes with `"hook":"pre-commit","ok":true`. The second note links to a missing `plan.md`, so the hook reports `"status":"failed"`, exits 1 and Git refuses the commit. `bf run validate` fails the same way; `logs/validate.log` holds the validation reply with `"error":"broken link: plan.md"` for `projects/launch.md`, then the failure line.

`bf validate` checks the working tree, including unstaged edits, not only the staged snapshot. A job with `emit: log`, the default, keeps its output in `logs/NAME.log`; `bf init` ignores `logs/` in Git. For `pre-push` hooks and pinned brains, see [Authoring](../../docs/docs/authoring.md) and [Getting started](../../docs/docs/getting-started.md).

## Use the weekly review in your brain

[Jobs](../../docs/docs/jobs.md) describes how to configure deterministic routines in `bf.yaml` (`emit: action`). The routine only reads pages: it never edits notes and links nothing, naming items by their `uri` or ref, because a link from a dated action would count as newer evidence for its target. An incomplete page makes it write nothing and exit non-zero.

Name the job `weekly-review` in `bf.yaml`, after its file, so it filters its own active run out of `attention` when computing whether other jobs need attention. Its changed notes, recent open actions and coming week come from the home page: recent actions are open (`status: draft`) actions whose folder date falls within the last seven days, up to today. When the 20-item preview of changed notes or upcoming items is full, or the 10 newest open actions all began within the period or later, the action names the `bf read` page that lists them all.

## React to new records

For whole sources without attribute filtering, prefer an event trigger directly in `bf.yaml`:

```yaml
jobs:
  watch-jira:
    prompt: prompts/watcher.md
    on: { event: { kind: record, source: jira } }
```

Use `records-gate.py` when you want scalar `--where ATTRIBUTE=VALUE` filtering or `--period` scoped exports on a scheduled job. Copy `records-gate.py` into `routines/` in your brain and wire it as the job's `gate:`:

```yaml
jobs:
  issue-triage:
    prompt: prompts/triage.md
    on: { every: 10m }
    gate:
      [
        python3,
        routines/records-gate.py,
        issues,
        --where,
        status=open,
        --since,
        "{{start}}",
        --until,
        "{{end}}",
        --state,
        "{{state}}",
      ]
```

Usage: `records-gate.py SOURCE [SOURCE...] [--where ATTRIBUTE=VALUE]... [--period PERIOD] [--since/--start ISO] --until/--end ISO --state DIR`.

- For each source, it runs `bf export --kind items --scope memories/SOURCE` and reads the lines as they arrive.
- It keeps the records BF [observed](../../docs/docs/reference.md) from 10 minutes before `--since` (or 24 hours before `--until` when `--since` is empty) until `--until`. The 10 minutes reach a collection that started before the previous check but finished after it.
- `--where status=open` keeps the records whose scalar `status` attribute reads `open`. Missing, list, or object attributes never match. Repeated `--where` conditions must all hold.
- When one or more unseen matching records are found, it persists them in `STATE/records-gate.json`, prints `{"events":[...]}`, and exits 0 so the gated job runs. When nothing matches, it prints `{"events": []}` and exits 1 so BF skips the run as `gated`.

Limits:

- **Installed bf:** the gate calls the first `bf` on `PATH`, needing bf 21.3.0 or later for `bf export --scope`.
- **Cost:** each check exports the whole source unless `--period` narrows it. Note that `--period 7d` filters by event `time`, not collection `observed` time.
- **Many changes:** a check lists at most 63 records within the 64 KiB event envelope and summarizes older matching records as a trailing `SOURCE:more` entry.
- **Failures:** invalid arguments, damaged `records-gate.json`, missing `bf`, non-empty `bf` stderr warnings, or failed exports print one `records-gate:` diagnostic line on stderr quoting no record, write nothing to state, and exit 2.
- **Memory:** remembered keys are forgotten 35 days after `--until`; deleting `records-gate.json` reports the window's records again. The file is capped at 64 MiB (dropping the oldest keys first).

The gate uses only Python 3.11's standard library and runs no shell.

## Audit evidence and job health

[evidence-health.py](evidence-health.py) inspects `bf status` and every page of `bf read review`, failing closed on retrieval problems, stale/missing caches, or non-advancing offsets. When open queue incidents, failed or uncertain deliveries, failing/overdue/silent/degraded jobs or sources, or review-flagged notes exist, it prints one draft `ACTION.md` on stdout (zero `- [ ]` checkboxes and zero Markdown links); when everything is healthy, it prints nothing.

```yaml
jobs:
  evidence-health:
    run: [routines/evidence-health.py, "{{brain}}", "{{end}}"]
    emit: action
    on: { every: 1d }
```
