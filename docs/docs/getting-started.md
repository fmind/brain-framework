# Getting started

By the end of this walkthrough, you can ask why a project made a decision, find the reason and read its source. You can follow the steps yourself or ask your agent to carry them out. Brain Framework runs on Linux and macOS.

## Install and create a brain

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Brain Framework. uv supplies a compatible Python version if needed.

```bash
uv tool install brain-framework
bf --version
```

If `bf` is not on PATH, run `uv tool update-shell` and open a new shell. See [Versions and upgrades](upgrades.md) when updating an existing installation.

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

The remaining examples run inside `~/brain` and use the name `brain`. Substitute your path and name if you chose differently. From elsewhere, select the folder explicitly:

```bash
bf read --brain ~/brain
```

You can also [register it by name](configuration.md#optional-machine-registration).

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

## Choose your next step

<span id="check-the-answers-your-team-needs"></span>
<span id="give-agents-access"></span>
<span id="connect-your-knowledge"></span>
<span id="join-a-team-brain"></span>
<span id="try-the-example"></span>
<span id="update-brain-framework"></span>
<span id="upgrade-from-brain-framework-12"></span>
<span id="upgrade-from-brain-framework-11"></span>

| You want to…                                        | Continue with…                                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Understand projects, memories, concepts and actions | [Core concepts](concepts.md), then [Files and notes](brain.md).                                        |
| Make sure important answers stay findable           | [Check your brain](checks.md): three cases for the decision above.                                     |
| Let an agent find the same decision                 | [Agent workflows](agents.md#install-the-skills), or the [Model Context Protocol (MCP) server](mcp.md). |
| Connect a decision to another note                  | [Linking knowledge](links.md).                                                                         |
| Collect a selected local document                   | [Your first sensor](sensors.md#your-first-sensor).                                                     |
| Share knowledge with teammates                      | [Team brains](team.md).                                                                                |
| Explore a complete fictional workflow               | The [runnable example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain).       |
| Update an existing installation                     | [Versions and upgrades](upgrades.md).                                                                  |

For guided setup, install `bf-setup` using the [skill installation guide](agents.md#install-the-skills).
