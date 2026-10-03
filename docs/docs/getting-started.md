---
description: Create a brain, save your first project decision, search its reason and verify the result.
---

# Getting started

Save a decision, find its reason and check that it stays findable. You need Linux or macOS, a terminal and a text editor. No provider account or model is needed; an agent can help with the same steps.

The example is a fictional New website project. BF prints compact JSON on one line; the JSON examples below show selected fields.

## Install and create a brain

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Brain Framework. `--python 3.14` selects the tested Python version, which uv downloads if needed.

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

If `bf` is not on PATH, run `uv tool update-shell` and open a new shell. [Install and update](upgrades.md#install) explains the Python choice and later updates.

### Choose a location

Create your brain in `~/brain`, the recommended location. `bf init` refuses a folder that already has content, so choose now if you prefer another folder or name.

<details markdown="1">
<summary>Use another folder or brain name (optional)</summary>

These are alternatives, not additional brains:

| Create it with                          | Folder             | Brain name   |
| --------------------------------------- | ------------------ | ------------ |
| `bf init ~/brain`                       | `~/brain`          | `brain`      |
| `bf init ~/team-brain`                  | `~/team-brain`     | `team-brain` |
| `bf init ~/brains/default --name brain` | `~/brains/default` | `brain`      |

The name defaults to the folder name, lowercased with accents removed and each run of other characters turned into one hyphen, except at either end: `Équipe produit` becomes `equipe-produit`. When that gives no name starting with a letter within 64 characters, or would drop a letter such as `ø`, pass `--name`. The name is stored in `bf.yaml` and identifies the brain in links, so keep it stable. The rest of this guide runs inside `~/brain`.

</details>

Skip this step if you already created the brain from the README:

```bash
bf init ~/brain
cd ~/brain
```

The new folder holds `bf.yaml` (the brain's configuration), `AGENTS.md` (instructions for agents), `projects/`, `concepts/` with a welcome note, `actions/` and `evals/retrieval.yaml` (saved questions that `bf eval` checks). No global configuration is needed. Run `git init` here if you want Git history.

## Save a decision

Create `projects/new-website.md` in your editor, or ask your agent to save this content. Replace `updated` with today's date:

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

The lines between `---` are the note's metadata. `type: project` says what the note is; `status: draft` marks its content as not yet reviewed. Work progress goes in the task list. [Write notes and actions](brain.md#notes) explains every field.

## Find its reason

```bash
bf search "visitors clear explanation"
```

Search returns JSON. Its `items` include the Decision section:

```json
{
  "ref": "projects/new-website.md#decision",
  "title": "New website — Decision"
}
```

A `ref` is an exact address. Pass it to `read`:

```bash
bf read projects/new-website.md#decision
```

The reply's `text` holds the original section:

```json
{
  "ref": "projects/new-website.md#decision",
  "text": "## Decision\n\nStart with a single product page because visitors need a clear explanation before signing up.\n\n"
}
```

Read the evidence before relying on a search excerpt. A reply with `problems` or `stale` is incomplete; see [incomplete answers](search.md#incomplete-answers-and-freshness).

## Check the brain

```bash
bf read projects
bf validate
bf eval
```

The projects page lists the note with its first open task, `"next":"Draft the product page."`. On this fresh brain, validation returns:

```json
{ "notes": 3, "problems": [], "records": 0, "valid": true }
```

A problem names the `file` to repair and its `error`; see [Check your brain](checks.md). `bf eval` runs the three starter checks, two questions about the welcome note and one check that an absent topic finds nothing, and returns `"score":"3/3"` and `"passed":true`. Add [three New website questions](checks.md#retrieval-cases) to check your own decision. Neither command runs a model.

Edit the project as work changes. Search notices edits automatically; update `updated` when the note's meaning changes.

**You now have a working brain.** Save real decisions the same way, or choose a [next step](#choose-your-next-step). Collection is optional.

## Collect your first source

<details markdown="1">
<summary>Optional exercise: collect a local brief, link it to your decision and notice when it changes</summary>

A sensor is a small program that prints JSON records; BF saves each record as evidence. This one reads a fictional local brief and dates it by the file's modification time. uv supplies Python 3.14, and the exercise needs no network access once Python is installed.

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
    # When the brief last changed: a later change flags the project linking to it.
    "attributes": {"updated": datetime.fromtimestamp(brief.stat().st_mtime, UTC).isoformat()},
}
print(json.dumps([record]))
```

A new brain has no `sensors:` key. Add this one at the top level of `bf.yaml`, keeping the other settings:

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

`bf.yaml` already has a `fields:` key: it declares four relations, the link meanings `author`, `owner`, `depends-on` and `related-to`. Add `kind` beneath them instead of creating a second `fields:` key:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  kind:
    description: Common kind of source item.
    type: string
    cardinality: one
    examples: [document]
```

- **`command`** runs the sensor from the brain folder, without a shell.
- **`fields:` in `bf.yaml`** declares `kind` as one string value, required on every record a sensor maps it to.
- **The sensor's `fields`** set `kind: document` on each record it prints.
- **`mode: snapshot`** says the output is the complete list, so a record the sensor stops printing is removed.
- **`refresh: 0`** keeps the sensor manual: it runs only when you collect it.

```bash
bf collect brief
bf search "product explanation" --scope memories/brief
bf read brief:website-brief
```

Collection reports `"records":1`. Search returns `brief:website-brief`, a record ref made of the sensor name and the record's id. Its exact read holds the saved item under `record`:

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

Validation now reports `"records":1` and `"valid":true`; the starter questions still pass.

Now change the evidence. Save `inputs/brief.txt` again with a revised brief:

```text
Visitors need a clear product explanation and pricing before signing up.
```

```bash
bf collect brief
bf read projects
```

Collection reports `"updated":1`: the record keeps its ref and holds the new text. The brief changed after the project's last edit, so the projects page flags the project linking to it, and `newer` names the record to read:

```json
{
  "ref": "projects/new-website.md",
  "review": true,
  "review_reasons": ["newer_evidence"],
  "newer": ["brief:website-brief"]
}
```

Read it with `bf read brief:website-brief`, then revise the decision or keep it: your next edit of the project clears the flag. [Add a sensor](sensors.md#your-first-sensor) replaces this tutorial sensor with a reviewed one.

</details>

## Choose your next step

Before adding more data, choose a recurring question, the sources that answer it and the person who keeps its project note current.

| You want to…          | Continue with…                                                                                |
| --------------------- | --------------------------------------------------------------------------------------------- |
| Understand the model  | [Core concepts](concepts.md).                                                                 |
| Write useful notes    | [Write notes and actions](brain.md).                                                          |
| Keep answers findable | [Check your brain](checks.md).                                                                |
| Use an agent          | Install the skills with `bf skills ~/.agents/skills`, then read [Agent workflows](agents.md). |
| Add external evidence | [Add a sensor](sensors.md), then keep it fresh with [Watch and schedule](schedule.md).        |
| Share with a team     | [Team setup](team.md).                                                                        |
| Fix a failed step     | [Troubleshooting](troubleshooting.md).                                                        |
