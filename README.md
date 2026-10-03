<p align="center">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-framework.svg"
    alt="Brain Framework: an exposed brain in an opening shell above twin aperture eyes"
    width="256"
    height="256"
  >
</p>

# Brain Framework 🧠

**🧠 Brain Framework: from information to informed actions.**

Collect information from your tools, connect it to your decisions and pick up work with the evidence at hand. Reusable context for you and your AI agents.

[![CI](https://github.com/fmind/brain-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fmind/brain-framework/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/brain-framework?color=174EA6)](https://pypi.org/project/brain-framework/) [![Python](https://img.shields.io/pypi/pyversions/brain-framework)](https://pypi.org/project/brain-framework/) [![License: MIT](https://img.shields.io/badge/license-MIT-174EA6)](https://github.com/fmind/brain-framework/blob/main/LICENSE)

Your meeting notes, issues and project decisions live in different places. Brain Framework (BF) brings selected information into a folder of Markdown notes and collected records you own. Search it across sessions, trace decisions to their sources and use the same context with different agents.

[Try it](https://github.com/fmind/brain-framework#try-it) · [Connect an agent](https://github.com/fmind/brain-framework#agents) · [Four-tool demo](https://fmind.github.io/brain-framework/docs/context-hub/) · [Documentation](https://fmind.github.io/brain-framework/)

## What can you do with it?

| When you need to…       | BF helps you…                                                       |
| ----------------------- | ------------------------------------------------------------------- |
| Resume a project        | Find the last verified outcome and next step.                       |
| Explain a decision      | Read the original reasoning and linked evidence.                    |
| Check work across tools | Follow explicit connections between briefs, issues and deployments. |
| Notice what changed     | See which conclusions newer evidence has overtaken.                 |

In the [runnable four-tool demo](https://fmind.github.io/brain-framework/docs/context-hub/), GitHub says the implementation is merged and Gcloud shows a healthy preview, but Jira still blocks launch on accessibility review. You record that conclusion in the project; when Jira later moves to Done, BF flags the project for review and names the issue to read. The records are fictional and everything runs locally. The [examples](https://github.com/fmind/brain-framework/tree/main/examples) also cover decision reviews, retrieval checks, hooks, routines, monitoring and teams.

## Try it

Start with one decision you can find again. You need Linux or macOS and [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs the tested Python 3.14 if needed ([install details](https://fmind.github.io/brain-framework/docs/upgrades/#install)). No model or provider account is required.

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

If your shell cannot find `bf`, run `uv tool update-shell` and open a new shell. Then create your brain:

```bash
bf init ~/brain
cd ~/brain
```

Create `projects/new-website.md` in your editor, or ask your agent to save this fictional decision. Use today's date for `updated`:

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

Find the reason, read its source and check the brain:

```bash
bf search "visitors clear explanation"
bf read projects/new-website.md#decision
bf validate
```

Search returns this match in `items` (other fields omitted):

```json
{ "ref": "projects/new-website.md#decision", "title": "New website — Decision" }
```

`read` returns the original Decision section with its note's status and date, and validation reports `"valid":true`. Edit the note and search again: BF notices changes on its own. The [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/) continues with a first collected source and a link from the decision to its evidence.

## Agents

Use the same brain with [Claude Code](https://code.claude.com/docs/en/overview), [Codex](https://openai.com/codex/), [GitHub Copilot](https://github.com/features/copilot) or another agent host. Give your terminal agent this prompt:

> Work in ~/brain and use the bf CLI. Find why we chose a single product page. Read the source, cite its ref and tell me the project's next action.

Check that it cites `projects/new-website.md#decision` and names “Draft the product page.” as the next task. For ongoing use, install the packaged skills into your host's skills folder:

```bash
bf skills ~/.agents/skills
```

Each host reads its own folder: for Claude Code, use `~/.claude/skills` instead. Claude Code reads `CLAUDE.md` rather than the brain's `AGENTS.md`: create a `CLAUDE.md` in the brain containing only `@AGENTS.md`.

`bf-use` finds evidence and keeps notes and actions current, `bf-setup` onboards brains and sources, and `bf-maintain` runs integrations, checks and upgrades. Hosts that prefer tools can call `search` and `read` through [MCP](https://fmind.github.io/brain-framework/docs/mcp/). See the [agent guide](https://fmind.github.io/brain-framework/docs/agents/).

## How it works

**Gather → normalize → organize and connect → act → learn.** Sensors, small programs you review, collect selected evidence; field mappings give records from different tools the same field names and explicit relations. You and your agents do the work, then keep the outcome and next step in the brain.

```text
brain/
├── bf.yaml      # configuration: fields, sensors and routines
├── AGENTS.md    # instructions for agents working in the brain
├── projects/    # what you're working on and why
├── concepts/    # what you've learned and can reuse
├── actions/     # work sessions to start, resume and review
├── evals/       # questions the brain must keep answering
├── memories/    # records collected by your sensors
└── logs/        # each program's recent output, ignored by Git
```

Notes are Markdown with structured metadata; each record is a JSON file. Open them in any editor and use Git for history; the search cache is disposable. [Links and backlinks](https://fmind.github.io/brain-framework/docs/links/) connect decisions to evidence. Every connection is explicit: BF never links two subjects because their names look alike. See the [file conventions](https://fmind.github.io/brain-framework/docs/brain/) and [team brains](https://fmind.github.io/brain-framework/docs/team/).

## Bring your tools

A **sensor** is a small program that reads a CLI, API or file and prints JSON records; BF validates, maps and saves them. Start with a reviewed example or write one for another source:

| Source                                    | Starting point                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Local files, highlights and Git           | [Document](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py), [highlight](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.py) and [Git history](https://github.com/fmind/brain-framework/blob/main/examples/sensors/git-history.py) sensors. |
| Google Calendar and Drive                 | [Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) and [Drive folder](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py) sensors using [`gws`](https://github.com/googleworkspace/cli).                                 |
| GitHub, Jira, Confluence and Google Cloud | The [GitHub history sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md), or your own using [`acli`](https://developer.atlassian.com/cloud/acli/), [`gcloud`](https://cloud.google.com/sdk/gcloud) or their APIs.                                                       |

Start with [one local source](https://fmind.github.io/brain-framework/docs/getting-started/#collect-your-first-source), then connect sources through [field mappings](https://fmind.github.io/brain-framework/docs/schema/#shared-fields-and-sensor-mappings). Provider tools handle authentication; your program selects what to collect. For example, [collect Calendar notes](https://fmind.github.io/brain-framework/docs/sensors/#from-meeting-notes-to-github-issues) and ask your agent to draft GitHub issues from them for your review.

After reviewing your sensors, run `bf watch` to keep them refreshed and see failures as they happen. [Routines](https://fmind.github.io/brain-framework/docs/routines/) run your own programs, such as a weekly review or a Git pre-commit validation. See [Watch and schedule updates](https://fmind.github.io/brain-framework/docs/schedule/) for timing, alerts and native schedules.

<a href="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/watch.svg">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/watch.svg"
    alt="Watch dashboard for the fictional offline demo: five sensors sorted by state, with a failed sensor's details beside their last success, next due time, item count and record changes."
    width="720"
  >
</a>

The dashboard shows the fictional [offline watch demo](https://github.com/fmind/brain-framework/tree/main/examples/watch), which needs no provider account.

## Fit and limits

BF fits projects where decisions and evidence must outlive a chat session. Start with one recurring question and the sources that answer it.

- **Your files, your control.** BF sends no telemetry or personal data to us. A connected cloud agent may send what it reads to its provider; see [privacy and security](https://fmind.github.io/brain-framework/docs/privacy/).
- **Offline retrieval.** Search and read never contact providers or run programs. Collect again when you need newer evidence; incomplete results say so.
- **People and agents supply the judgment.** BF matches words and explicit identities, runs no model and learns nothing on its own. See [what BF does not do](https://fmind.github.io/brain-framework/docs/concepts/#what-bf-does-not-do).

[Browse the commands](https://fmind.github.io/brain-framework/docs/commands/) · [Report an issue](https://github.com/fmind/brain-framework/issues) · [Contribute](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md) · [MIT licensed](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
