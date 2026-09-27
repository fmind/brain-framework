# Example routines

Standalone, deterministic routines. Copy the ones you need into a brain's `routines/`, declare them in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Routine            | Arguments   | Action                                                                                                                                                                                 |
| ------------------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `weekly-review.py` | `BRAIN END` | Projects due for review across the paginated project listing, a counted open-task preview, the last seven days' activity by source, changed notes, recent actions and the coming week. |

```yaml
# https://fmind.github.io/brain-framework/
version: 6
name: brain
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
```

After reviewing and configuring the script, preview it without creating an action. Run from the brain directory with Python 3.14 available; replace the timestamp with the review time:

```bash
routines/weekly-review.py ~/brain 2026-09-27T12:00:00Z
```

If there is something to review, the output starts with OKF action metadata (`type: action`, `status: draft`), followed by project checkboxes and recent activity. Empty output means there is no review to prepare. The timestamp sets the note's date; page reads use the current brain state.

```bash
bf update --dry-run --brain ~/brain
bf update --brain ~/brain
bf read actions --brain ~/brain
```

`update --dry-run` lists due work without running it. A due routine runs after sensors and, when its output is nonempty, creates `actions/YYYY-MM-DD_weekly-review/ACTION.md`. Read the path returned by the update. An existing action is never replaced.

## Contract

`tests/test_adapters_routines.py` checks every example with a fake `bf` executable.

- Python 3.14 standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Deterministic: the same pages produce the same Markdown. No model, network or provider call; read the brain through `bf read` and `bf search` with literal argv.
- Stdout is one OKF action or empty. BF validates its metadata and declared links before writing. Use `status: draft|stable|deprecated` for note maturity and checkboxes for work progress.
- Link only notes needing review, using returned `uri` values: links from a dated action count as newer evidence for their targets. Name other notes and collected records by ref; do not copy record titles into authored context.
- A failed or incomplete page (`problems`, `stale`) exits nonzero with nothing on stdout and one generic sentence on stderr; Brain Framework then writes nothing and retries at the next due run.
- Project review follows every `next_offset`, up to 100 pages (20,000 projects). A stalled continuation or exceeded limit fails before writing; the home page alone cannot establish that all projects are current. Other home-page sections remain previews.
- Task counts cover the eligible note selection; up to ten open tasks are previewed with source refs and lines. Plain bullets and code-span refs avoid duplicating tasks or triggering new-evidence reminders. Follow `bf read tasks` continuations for the complete list.
- A routine never edits notes: people and agents read its action and update the owning notes.
