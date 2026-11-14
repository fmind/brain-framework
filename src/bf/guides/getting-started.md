# Getting started — install, initialize, search, collect, register, and connect

Create a brain, save your first project decision, search its reason, collect a local source, register the brain, install `/bf`, and connect read-only MCP on Linux or macOS (WSL2 on Windows).

## Install and create a brain

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Brain Framework (Python 3.14+):

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

Include optional MCP support when connecting IDE or desktop hosts (`uv tool install --python 3.14 'brain-framework[mcp]'`). If `bf` is missing from `PATH`, run `uv tool update-shell` and open a new shell.

### Choose a location

Create your brain in `~/brain` (`bf init` refuses non-empty folders; pass `--name NAME` if the directory name lacks an ASCII letter slug):

```bash
bf init ~/brain
cd ~/brain
```

`bf init` writes `bf.yaml` (with an editor schema modeline), `.gitignore`, `AGENTS.md`, `concepts/index.md`, `concepts/welcome.md`, `projects/`, `actions/`, and `evals/retrieval.yaml`.

## Save a decision

Create `projects/new-website.md` in your editor:

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

`type: project` sets the note kind and `status: draft` marks review maturity; tasks stay in Markdown checkboxes ([Authoring](authoring.md)).

## Find its reason

```bash
bf search "visitors clear explanation"
```

Search returns JSON matching the exact section:

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

Always inspect exact refs plus `problems` or `stale` before relying on search excerpts ([Retrieval](retrieval.md)).

## Check the brain

```bash
bf read projects
bf validate
bf eval
```

`bf read projects` lists `"next":"Draft the product page."`. Validation returns:

```json
{ "notes": 3, "problems": [], "records": 0, "valid": true }
```

`bf eval` runs `evals/retrieval.yaml` offline and reports `"score":"3/3"` and `"passed":true`.

## Collect your first source

<details markdown="1">
<summary>Optional exercise: collect a local brief, link it to your decision, and notice when it changes</summary>

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
    "attributes": {
        "kind": "document",
        "updated": datetime.fromtimestamp(brief.stat().st_mtime, UTC).isoformat(),
    },
}
print(json.dumps([record]))
```

A new brain has no `jobs:` key. Add this one at the top level of `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/jobs/
jobs:
  brief:
    run: [uv, run, --no-project, --python, "3.14", sensors/brief.py]
    emit: records
    mode: snapshot
```

```bash
bf collect brief
bf search "product explanation" --scope memories/brief
bf read brief:website-brief
```

Collection reports `"records":1`, and reading `brief:website-brief` returns:

```json
{
  "ref": "brief:website-brief",
  "record": {
    "id": "website-brief",
    "text": "Visitors need a clear product explanation before signing up.\n",
    "attributes": { "kind": "document" }
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

Collection reports `"updated":1`, and `bf read projects` flags `newer_evidence` (`bf read review` aggregates this across the brain):

```json
{
  "ref": "projects/new-website.md",
  "review": true,
  "review_reasons": ["newer_evidence"],
  "newer": ["brief:website-brief"]
}
```

Continue in [Jobs](jobs.md#your-first-sensor) to replace this tutorial script with a reviewed sensor.

</details>

## Register, install `/bf`, and connect MCP

Register a brain in `~/.config/bf/config.yaml` (never inside the brain repo) so commands can select it by name or default from any folder, install the packaged `/bf` skill, and connect read-only MCP:

```text
bf register ~/brain --name work --default
bf unregister work
bf skill install                   # local brain skills/bf linked to .agents/skills/bf and .claude/skills/bf
bf skill install --global          # ~/.agents/skills/bf (links ~/.claude/skills/bf if present)
bf skill install --check           # verify drift without writing
bf mcp --brain ~/brain             # stdio server exposing offline search and read only
```

Configure MCP hosts to spawn `bf mcp --brain ~/brain` (requiring `'brain-framework[mcp]'`), then verify `search` for `"welcome"` and `read` on `concepts/welcome.md`.

## Upgrade and pinned runtimes

Upgrade globally with `uv tool upgrade brain-framework` (or `--reinstall --managed-python --python 3.14 'brain-framework[mcp]'`), then re-run `bf skill install --global`. Drain `bf queue` and follow any breaking steps in the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md) before upgrading across majors.

**Pinned runtime rule**: always run global `bf` on `PATH` by default. Run `uv run --locked bf ...` only when the brain carries `pyproject.toml`/`uv.lock` AND `bf status` shows it registered (`registered: true`); never run `uv run` on unregistered brains or downloaded clones.

## Choose your next step

| Goal                                 | Guide                                                     |
| ------------------------------------ | --------------------------------------------------------- |
| Understand the model and boundaries  | [Overview](index.md)                                      |
| Author notes, actions, and relations | [Authoring](authoring.md)                                 |
| Search, read pages, and export graph | [Retrieval](retrieval.md)                                 |
| Add sensors, `bf ingest`, routines   | [Jobs](jobs.md)                                           |
| Configure prompts, events, and watch | [Assist](assist.md)                                       |
| Check health, recover, and inspect   | [Operations](operations.md) and [Reference](reference.md) |
