# Getting started

By the end, you will have a project decision, one collected source record and a schema mapping. You can search them and read the evidence. You can follow the steps yourself or ask your agent to carry them out. Brain Framework runs on Linux and macOS.

All examples run inside your brain directory. No provider account or model is needed.

## Install and create a brain

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Brain Framework. uv supplies a compatible Python version if needed.

```bash
uv tool install brain-framework
bf --version
```

If `bf` is not on PATH, run `uv tool update-shell` and open a new shell. See [Install and update](upgrades.md) when updating an existing installation.

Create your personal brain in `~/brain`, the recommended starting location. Skip initialization if you already created it from the README:

```bash
bf init ~/brain
cd ~/brain
```

No global configuration is required. The new directory contains starter notes, `tests/` for your technical tests and `evals/retrieval.yaml` for retrieval checks. You can run `git init` here if you want Git history.

## Choose a location

The folder is yours to choose. These are alternatives, not additional required brains:

| Create it with                          | Folder             | Name in BF links |
| --------------------------------------- | ------------------ | ---------------- |
| `bf init ~/brain`                       | `~/brain`          | `brain`          |
| `bf init ~/team-brain`                  | `~/team-brain`     | `team-brain`     |
| `bf init ~/brains/default --name brain` | `~/brains/default` | `brain`          |

`bf init` requires a path; `~/brain` is a recommendation, not an automatic selection. The name defaults to the last directory component. It lives in `bf.yaml` and appears in addresses such as `bf://brain/projects/new-website.md`; keep it stable when moving or sharing the folder.

The remaining examples run inside `~/brain` and use the name `brain`. Substitute your path and name if you chose differently. BF discovers the enclosing brain automatically; [selection options](configuration.md#select-a-brain) are only needed when working elsewhere or choosing a different brain.

## Save a decision

Create `projects/new-website.md` in your editor, or ask your agent to save this content. It is a fictional project; replace `updated` with today's date:

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

The lines between `---` are the note's metadata. `type: project` identifies the kind of note; `status: draft` marks its knowledge as unreviewed. Work progress goes in the task list. [Files and notes](brain.md#notes) explains the format.

## Find its reason

```bash
bf search "visitors clear explanation"
```

Search returns JSON. In `items`, find the Decision section with these fields (other fields omitted):

```json
{
  "ref": "projects/new-website.md#decision",
  "title": "New website — Decision",
  "uri": "bf://brain/projects/new-website.md#decision"
}
```

Pass the returned `ref` to `read`. The `uri` names the brain too, which distinguishes sources when searching several brains.

```bash
bf read projects/new-website.md#decision
```

The reply's `text` contains the original section:

```json
{
  "text": "## Decision\n\nStart with a single product page because visitors need a clear explanation before signing up.\n\n"
}
```

Read the evidence before relying on a search excerpt. If a reply includes `problems` or `stale`, follow the [retrieval guidance](search.md#incomplete-answers-and-freshness) before treating it as complete.

## Check the brain

```bash
bf read projects
bf validate
bf eval
```

The projects page includes `"next":"Draft the product page."`. On this fresh brain, validation returns:

```json
{ "notes": 3, "problems": [], "records": 0, "valid": true }
```

`bf eval` returns `"score":"3/3"` and `"passed":true` for the starter welcome-note checks. Add the [three New website cases](checks.md#retrieval-cases) to check your own decision too. Neither command runs an LLM.

Edit the project as work changes. Search notices edits automatically; refresh `updated` when you change the note's meaning.

## Collect your first source

A sensor is a small program that prints JSON records. This one reads a fictional local brief. It uses `python3` and needs no dependencies or network access.

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
from pathlib import Path

text = Path("inputs/brief.txt").read_text(encoding="utf-8")
print(json.dumps([{"id": "website-brief", "title": "Product brief", "text": text}]))
```

In `bf.yaml`, replace `sensors: {}` with:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  brief:
    command: [python3, sensors/brief.py]
    refresh: 0
    fields:
      kind: { value: document }
```

Merge this into the existing `schema:` mapping, keeping the starter roles alongside `kind`. Do not create a second `schema:` key:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
schema:
  kind:
    description: Common kind of source item.
    type: string
    cardinality: one
    examples: [document]
```

- **Sensor:** reads the selected file and emits its stable id, title and text.
- **Schema:** declares that `kind` is a required string for this mapping.
- **Mapping:** adds `kind: document` to each record from `brief`.
- **`refresh: 0`:** keeps collection manual.

```bash
bf collect brief
bf search "product explanation" --scope memories/brief
bf read brief:website-brief
```

Collection reports `"records":1`. Search returns `brief:website-brief`; its exact read includes these record fields:

```json
{
  "id": "website-brief",
  "text": "Visitors need a clear product explanation before signing up.\n",
  "fields": { "kind": "document" }
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

Validation now reports one record and `"valid":true`; the starter evaluation still passes. Edit the input and collect again to update the same record without changing its ref. For imports beyond this tiny example, use the reviewed, bounded adapters in [Sensors](sensors.md).

## Choose your next step

Before adding more data, choose a recurring question, its authoritative sources and the person who will keep its project note current. For shared work, follow [Team setup](team.md).

| You want to…                                        | Continue with…                                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Understand projects, memories, concepts and actions | [Core concepts](concepts.md), then [Files and notes](brain.md).                                        |
| Make sure important answers stay findable           | [Check your brain](checks.md): three cases for the decision above.                                     |
| Let an agent find the same decision                 | [Agent workflows](agents.md#install-the-skills), or the [Model Context Protocol (MCP) server](mcp.md). |
| Connect a decision to another note                  | [Linking knowledge](links.md).                                                                         |
| Collect a selected local document                   | [Your first sensor](sensors.md#your-first-sensor).                                                     |
| Share knowledge with teammates                      | [Team brains](team.md).                                                                                |
| Explore a complete fictional workflow               | The [runnable example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain).       |
| Update an existing installation                     | [Install and update](upgrades.md).                                                                     |

For guided setup, install `bf-setup` using the [skill installation guide](agents.md#install-the-skills).

## Keep information refreshed

After adding and reviewing your [sensors](sensors.md), start `bf watch`. This is the primary refresh mode: it runs due programs, shows local status and reports new failures and recovery. Defaults work without a preferences file; use `bf status --watch` to observe without execution. Optional `bf schedule` generates native timer files for unattended checks. See [Watch and schedule updates](schedule.md) for the runnable offline demo, selection and platform limits.
