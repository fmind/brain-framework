---
icon: lucide/rocket
description: Create a brain, save your first project decision, search its reason and verify the result.
---

# Getting started

Save a decision, find its reason and check that it stays findable on Linux or macOS (use WSL2 on Windows). No provider account or model is required; BF prints compact JSON on one line.

## Install and create a brain

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Brain Framework:

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

If `bf` is not on `PATH`, run `uv tool update-shell` and open a new shell ([install and update](upgrades.md#install)).

### Choose a location

Create your brain in `~/brain` (`bf init` refuses a non-empty folder; pass `--name NAME` when the folder name has no ASCII letter slug, as [commands](commands.md) details):

```bash
bf init ~/brain
cd ~/brain
```

`bf init` writes `bf.yaml` (with an [editor schema](schema.md#editor-schemas) modeline), `AGENTS.md`, `projects/`, `concepts/`, `actions/`, `evals/retrieval.yaml` and `.gitignore`. Commands act on the enclosing brain unless `BF_BRAIN` or `--brain` overrides it ([selection](configuration.md#select-a-brain)).

## Save a decision

Create `projects/new-website.md` in your editor (replace `updated` with today's date):

```markdown
---
type: project
status: draft
updated: 2026-09-27
summary: Launch a product website that helps visitors understand the product.
---

# New website

## Decision

Start with a single product page because visitors need a clear explanation before signing up.

## Next actions

- [ ] Draft the product page.
```

`type: project` names the note kind and `status: draft` marks its review maturity; task progress lives in checkboxes ([notes](brain.md#notes)).

## Find its reason

```bash
bf search "visitors clear explanation"
```

Search returns JSON whose `items` include the section:

```json
{
  "ref": "projects/new-website.md#decision",
  "title": "New website — Decision"
}
```

Pass the exact `ref` to `read`:

```bash
bf read projects/new-website.md#decision
```

```json
{
  "ref": "projects/new-website.md#decision",
  "text": "## Decision\n\nStart with a single product page because visitors need a clear explanation before signing up.\n\n"
}
```

Always read the exact ref before relying on an excerpt, and inspect `problems` or `stale` ([incomplete answers](retrieval.md#incomplete-answers-and-freshness)).

## Check the brain

```bash
bf read projects
bf validate
bf eval
```

`bf read projects` lists the note with `"next":"Draft the product page."`. Validation reports:

```json
{ "notes": 3, "problems": [], "records": 0, "valid": true }
```

`bf eval` runs the starter checks and returns `"score":"3/3"` and `"passed":true` ([add your own cases](checks.md#retrieval-cases)).

## Collect your first source

<details markdown="1">
<summary>Optional exercise: collect a local brief, link it to your decision and notice when it changes</summary>

```bash
mkdir -p inputs sensors
```

Save `inputs/brief.txt`:

```text
Visitors need a clear product explanation before signing up.
```

Save `sensors/brief.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

brief = Path("inputs/brief.txt")
record = {
    "id": "website-brief",
    "title": "Product brief",
    "text": brief.read_text(encoding="utf-8"),
    "attributes": {"updated": datetime.fromtimestamp(brief.stat().st_mtime, UTC).isoformat()},
}
print(json.dumps([record]))
```

A new brain has no `sensors:` key. Add this one at the top level of `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  brief:
    command: [uv, run, --no-project, --python, "3.14", sensors/brief.py]
    mode: snapshot
    refresh: 0
    fields:
      kind: { value: document }
```

`mode: snapshot` treats stdout as the full catalog, `refresh: 0` keeps runs manual, and `fields` sets the [standard `kind` field](schema.md#standard-vocabulary):

```bash
bf collect brief
bf search "product explanation" --scope memories/brief
bf read brief:website-brief
```

Collection reports `"records":1`, and the exact read returns:

```json
{
  "ref": "brief:website-brief",
  "record": {
    "id": "website-brief",
    "text": "Visitors need a clear product explanation before signing up.\n",
    "fields": { "kind": "document" }
  }
}
```

Add a source link beneath the project's Decision section:

```markdown
Evidence: [Product brief](brief:website-brief).
```

```bash
bf validate
bf eval
```

Validation reports `"records":1` and `"valid":true`. Now revise `inputs/brief.txt` — Save `inputs/brief.txt`:

```text
Visitors need a clear product explanation and pricing before signing up.
```

```bash
bf collect brief
bf read projects
```

Collection reports `"updated":1`, and `bf read projects` flags the project because linked evidence changed after its last edit (`bf read review` surfaces the same queue across the brain):

```json
{
  "ref": "projects/new-website.md",
  "review": true,
  "review_reasons": ["newer_evidence"],
  "newer": ["brief:website-brief"]
}
```

Read `brief:website-brief` and update the project to clear the flag. [Add a sensor](sensors.md#your-first-sensor) replaces this tutorial script with a reviewed sensor.

</details>

## Choose your next step

| Goal                                 | Guide                                                                |
| ------------------------------------ | -------------------------------------------------------------------- |
| Understand the model                 | [Core concepts](concepts.md)                                         |
| Author notes and track work sessions | [Write notes and actions](brain.md)                                  |
| Connect an agent and install skills  | `bf skills ~/.agents/skills`, then [Agent workflows](agents.md)      |
| Collect sources and watch schedules  | [Add a sensor](sensors.md) and [Watch and schedule](schedule.md)     |
| Wake your harness on new evidence    | [Assistants](prompts.md) and [Four-tool walkthrough](context-hub.md) |
| Collaborate or troubleshoot          | [Team setup](team.md) and [Troubleshooting](troubleshooting.md)      |
