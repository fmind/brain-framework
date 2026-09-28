---
description: Create a brain, save your first project decision, search its reason and verify the result.
---

# Getting started

Save a decision, find its reason and verify that it remains readable. You need Linux or macOS, a terminal and a text editor; an agent can help with the same steps. No provider account or model is needed.

The example is a fictional New website project. Run commands inside `~/brain` after creating it. JSON examples show selected fields; BF prints compact JSON on one line.

## Install and create a brain

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Brain Framework. `--python 3.14` selects the tested Python version, which uv downloads if needed.

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

If `bf` is not on PATH, run `uv tool update-shell` and open a new shell. See [Install and update](upgrades.md#install) for the Python choice and for updating an existing installation.

### Choose a location

Create your personal brain in `~/brain`, the recommended starting location. `bf init` refuses a folder that already has content, so choose now if you prefer another folder or name.

<details markdown="1">
<summary>Use another folder or brain name (optional)</summary>

These are alternatives, not additional required brains:

| Create it with                          | Folder             | Name in BF links |
| --------------------------------------- | ------------------ | ---------------- |
| `bf init ~/brain`                       | `~/brain`          | `brain`          |
| `bf init ~/team-brain`                  | `~/team-brain`     | `team-brain`     |
| `bf init ~/brains/default --name brain` | `~/brains/default` | `brain`          |

The name defaults to the directory name, lowercased, with other characters replaced by hyphens, such as `my-brain` for `~/My Brain`; it must start with a letter and fit 64 characters, otherwise pass `--name`. It lives in `bf.yaml` and appears in addresses such as `bf://brain/projects/new-website.md`; keep it stable when moving or sharing the folder.

The remaining examples run inside `~/brain` and use the name `brain`. Substitute your path and name if you chose differently. BF discovers the enclosing brain automatically; [selection options](configuration.md#select-a-brain) are only needed when working elsewhere or choosing a different brain.

</details>

Skip initialization if you already created the brain from the README:

```bash
bf init ~/brain
cd ~/brain
```

No global configuration is required. The new directory contains starter notes, `tests/` for your technical tests and `evals/retrieval.yaml` for retrieval checks. You can run `git init` here if you want Git history.

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

The lines between `---` are the note's metadata. `type: project` identifies the kind of note; `status: draft` marks its knowledge as unreviewed. Work progress goes in the task list. [Write notes and actions](brain.md#notes) explains the format.

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

Pass the returned `ref` to `read`:

```bash
bf read projects/new-website.md#decision
```

The reply's `text` contains the original section:

```json
{
  "ref": "projects/new-website.md#decision",
  "text": "## Decision\n\nStart with a single product page because visitors need a clear explanation before signing up.\n\n"
}
```

The `uri` also names the brain. Once you search [several brains](configuration.md#related-brains), read the `uri` instead: a plain ref that exists in two brains fails and asks for that `bf://` address.

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

A problem names the `file` to repair and its `error`; see [Check your brain](checks.md). `bf eval` returns `"score":"3/3"` and `"passed":true` for the starter welcome-note checks. Add the [three New website cases](checks.md#retrieval-cases) to check your own decision too. Neither command runs an LLM.

Edit the project as work changes. Search notices edits automatically; refresh `updated` when you change the note's meaning.

**You now have a working brain.** Save real decisions in the same way, or choose a [next step](#choose-your-next-step). Collection is optional.

## Collect your first source

<details markdown="1">
<summary>Optional exercise: collect a local brief and link it to your decision</summary>

A sensor is a small program that prints JSON records. This one reads a fictional local brief. It uses uv to supply Python 3.14 and needs no additional Python dependencies. Once Python is installed, this exercise needs no network access.

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
- **`mode: snapshot`:** the output is the complete catalog, so a record the sensor stops returning is removed.
- **`refresh: 0`:** keeps collection manual.

```bash
bf collect brief
bf search "product explanation" --scope memories/brief
bf read brief:website-brief
```

Collection reports `"records":1`. Search returns `brief:website-brief`; its exact read holds the saved item under `record`:

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

Validation now reports `"records":1` and `"valid":true`; the starter evaluation still passes. Edit the input and collect again to update the same record without changing its ref. [Add a sensor](sensors.md#your-first-sensor) replaces this tutorial sensor with a reviewed, bounded one.

</details>

## Choose your next step

Before adding more data, choose a recurring question, its authoritative sources and the person who will keep its project note current. For shared work, follow [Team setup](team.md).

| You want to…          | Continue with…                                                          |
| --------------------- | ----------------------------------------------------------------------- |
| Write useful notes    | [Core concepts](concepts.md), then [Write notes and actions](brain.md). |
| Keep answers findable | [Check your brain](checks.md): three cases for this decision.           |
| Use an agent          | [Agent workflows](agents.md) or [MCP setup](mcp.md).                    |
| Add external evidence | [Add a sensor](sensors.md) or [team setup](team.md).                    |
| Fix a failed step     | [Troubleshooting](troubleshooting.md).                                  |

For guided setup, install `bf-setup` using the [skill installation guide](agents.md#install-the-skills).

## Keep information refreshed

After adding and reviewing your [sensors](sensors.md), start `bf watch` inside the brain. This is the primary refresh mode: it runs due programs, shows local status and reports new failures and recovery. Defaults work without a preferences file; use `bf status --watch` to observe without execution. Optional `bf schedule` generates native timer files for unattended checks. See [Watch and schedule updates](schedule.md) for the runnable offline demo, selection and platform limits.
