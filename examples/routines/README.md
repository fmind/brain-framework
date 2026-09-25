# Example routines

Standalone, deterministic routines. Copy the ones you need into a brain's `routines/`, declare them in `bf.yaml`, and adapt and test them there; they then belong to the brain. The Python package neither bundles nor installs them.

| Routine            | Arguments   | Action                                                                                                                                                                         |
| ------------------ | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `weekly-review.py` | `BRAIN END` | Projects due for review (old notes or newer linked evidence) with their next task, the last seven days' activity by source, changed notes, recent actions and the coming week. |

```yaml
# https://fmind.github.io/brain-framework/
version: 5
name: knowledge
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
```

`bf update` runs a due routine after the brain's sensors and writes its output to `actions/YYYY-MM-DD_weekly-review/ACTION.md`. Run the script directly to preview it: `routines/weekly-review.py ~/brain 2026-09-25T00:00:00Z`.

## Contract

`tests/test_adapters_routines.py` checks every example with a fake `bf` executable.

- Python 3.14 standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Deterministic: the same pages produce the same Markdown. No model, network or provider call; read the brain through `bf read` and `bf search` with literal argv.
- Stdout is one Markdown note with valid frontmatter, or nothing when there is nothing to review. Brain Framework validates its frontmatter and declared links before writing. Link only what needs review, using the `uri` values that pages return; name other notes by ref, because a link from a dated action counts as newer evidence for the note it links to. Name `external` items by ref only: their titles are text other people wrote, and an action is authored knowledge.
- A failed or incomplete page (`problems`, `stale`) exits nonzero with nothing on stdout and one generic sentence on stderr; Brain Framework then writes nothing and retries at the next due run.
- A routine never edits notes: people and agents read its action and update the owning notes.
