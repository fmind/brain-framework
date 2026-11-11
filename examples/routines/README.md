# Example routines

Standalone, deterministic brain programs. Copy the ones you need into a brain, declare them under `routines:` in `bf.yaml` or as an assistant schedule's `gate`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Routine            | Configuration                            | Result                                                                                                                                                      |
| ------------------ | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `_bf.py`           | Copy beside routines, gates or channels  | Shared standard-library kit: windows, private state, bounded `bf` calls, pagination, exports, text sanitizers, gate events, OKF actions and channel JSON.   |
| `weekly-review.py` | `output: action`, weekly `refresh`       | A draft action listing programs needing attention, projects due for review, a counted open-task preview, the last seven days by source and the coming week. |
| `bf validate`      | `hooks: [pre-commit]`, no script to copy | A Git commit stops while the brain has validation problems; the reply stays in `logs/validate.log`.                                                         |
| `records-gate.py`  | An assistant schedule's `gate`           | One event for each record a source gained or changed since the last check, reported once; without events, the harness does not start.                       |

The weekly review and pre-commit walkthroughs run from the framework checkout after `uv sync --locked`, in a subshell that isolates configuration and state and removes its temporary files on exit.

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
  cp examples/routines/_bf.py examples/routines/weekly-review.py "$routine_demo/brain/routines/"
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

The dry run lists the due sensor and routine. The update collects one fictional record, then reports the routine with `"status":"ran"` and an `action` at `actions/YYYY-MM-DD_weekly-review-XXXXXXXX/ACTION.md`, where `XXXXXXXX` is a random suffix. The action counts the seven open tasks across three notes and lists each with its section ref and line, then counts the period's records by source: `records by source: demo 1`. It says `No project note is due for review.`: the copied project is newer than the demo record it links, and its `stale_after` falls due on 2026-12-18, after which the action lists it with `review due 2026-12-18`. [Prepare a weekly review](../../docs/docs/routines.md#prepare-a-weekly-review) describes the full layout and ordering. Validation returns `"valid":true`. A second update within the week creates nothing. Read the returned action to continue the review: the routine completes no task for you.

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

Name the routine `weekly-review` in `bf.yaml`, after its file: [Prepare a weekly review](../../docs/docs/routines.md#prepare-a-weekly-review) explains why. Its changed notes, recent open actions and coming week come from the home page: recent actions are open (`status: draft`) actions whose folder date falls within the last seven days, up to today. When the 20-item preview of changed notes or upcoming items is full, or the 10 newest open actions all began within the period or later, the action names the `bf read` page that lists them all.

## React to new records

`records-gate.py` turns a frequent assistant schedule into an event-driven one: your harness runs only when a source gained or changed records. The [four-tool walkthrough](../../docs/docs/context-hub.md#hear-of-each-change-once) runs it on a fictional Jira record. [Gates](../../docs/docs/prompts.md#gates) describes what BF does with its events.

Copy `records-gate.py` and `_bf.py` to `routines/` in your brain and name `records-gate.py` as a schedule's gate. For sources without field-level `--where` filters, a declarative `when: { sources: [issues] }` schedule needs no script at all; use `records-gate.py` when you want single-value `--where` filtering or period-scoped exports. This schedule checks an `issues` source, collected by the [GitHub issues sensor](../sensors/README.md#github-issues), every 10 minutes and reports open issues only:

```yaml
schedules:
  issues: { every: 600, gate: { command: [python3, routines/records-gate.py, issues, --where, status=open] } }
```

Usage: `records-gate.py SOURCE [SOURCE...] [--where FIELD=VALUE]... [--period PERIOD]`.

- For each source, it runs `bf export --kind items --scope memories/SOURCE` and reads the lines as they arrive.
- It keeps the records BF [observed](../../docs/docs/schema.md#record-revisions-and-provenance), meaning first collected or seen changed, from 10 minutes before the window's start until its end. The 10 minutes reach a collection that started before the previous check but finished after it.
- `--where status=open` keeps the records whose `status` field reads `open`, as that sensor prints it. A field the source's sensor does not print never matches, so check a record with `bf read` before filtering on it. Repeated conditions must all hold. Numbers and booleans read as JSON writes them, such as `1` or `true`.
- It prints one event per record, newest first, and remembers each one in `BF_STATE_DIR/records-gate.json`, so the harness hears of a record again only when it changes:

```json
{
  "events": [
    { "key": "bf://brain/mail:m1@2026-09-29T10:30:00.000000Z", "text": "Launch moved", "ref": "bf://brain/mail:m1" }
  ]
}
```

The `key` names the record's version, `text` its title on one line of up to 280 characters, and `ref` the record's `bf://` address, which the harness reads with `bf read`. The title is collected evidence: the gate makes hidden characters visible, but the harness must still treat it as data.

Limits:

- **Installed bf:** the gate calls the first `bf` on PATH, which may differ from a pinned brain's runtime. It needs bf 21.3.0 or later for `bf export --scope`, and names an older version.
- **Cost:** each check exports the whole source. On 2026-11-08, on a 12-core aarch64 Linux machine, a synthetic source of 10,000 records with 1 KiB of text each exported 12.6 MB in 1.0–1.1 seconds, and each gate check took 1.0–1.2 seconds. Keep `every` at 300 seconds or more for such a source. A check fails after 100 seconds, even when the gate's `timeout` is longer; a shorter `timeout` stops the gate and its export sooner.
- **Referenced brains:** the export also lists the source's records in the brains this brain [references](../../docs/docs/configuration.md#related-brains), and the gate reports them too. A plain ref such as `mail:m1` can exist in several of them, so each event names its record by address.
- **Periods:** `--period 7d` exports less, but a period places records by their event `time`, not by when BF observed them ([time meanings](../../docs/docs/retrieval.md#time-meanings)). A record collected late with an older date then never reaches the gate.
- **Fields:** `--where` reads the export's `fields`, which list declared single-value fields of up to 200 characters ([types and cardinality](../../docs/docs/schema.md#types-and-cardinality)), up to 2 KiB in all, in field-name order ([limits](../../docs/docs/limits.md)). A `cardinality: many` field, a longer value or a field left out once a record's fields pass 2 KiB never matches.
- **Records written by hand** have no `observed` time, so the gate ignores them.
- **Collection lag:** a collection that runs longer than 10 minutes, beyond the default 300-second sensor timeout, can finish after the checks that could still reach its records.
- **Many changes:** a check reports at most 63 records, within the 64 KiB BF accepts. One more event, such as `mail:more`, counts the older ones and points to the source page.
- **Failures:** when bf is missing, the export fails, prints an invalid line or warns on stderr, such as about a busy cache, the gate prints one `records-gate:` line that quotes no record, remembers nothing and exits 1. BF then retries it. A missing `BF_START`, `BF_END` or `BF_STATE_DIR` exits 2.
- **Brain problems:** an invalid file anywhere in the brain or a referenced brain, such as a typo in an unrelated note's frontmatter, or an unavailable referenced brain, fails even a scoped export, and so every check. BF retries the gate up to 20 times, for about an hour. The job then waits in `bf queue` and blocks the schedule ([schedule and overlap](../../docs/docs/prompts.md#schedule-and-overlap)): fix the file, then run `bf queue ID --retry`.
- **Memory:** remembered records are forgotten 35 days after BF observed them. Deleting `records-gate.json` reports the current window's records again. The file holds up to 64 MiB: beyond that, the gate forgets the oldest records first and says so on stderr.

The gate uses only Python 3.11's standard library and runs no shell. Its tests, in `tests/test_adapters_gates.py`, run it against a fake `bf` and inside a real `bf update`.
