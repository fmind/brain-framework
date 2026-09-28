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

Your meeting notes, issues and project decisions live in different places. Brain Framework (BF) brings selected information into a folder of Markdown notes and source records you own. Search it across sessions, trace decisions to their sources and use the same context with different agents.

[Try it](https://github.com/fmind/brain-framework#try-it) · [Connect an agent](https://github.com/fmind/brain-framework#agents) · [Four-tool demo](https://fmind.github.io/brain-framework/docs/context-hub/) · [Documentation](https://fmind.github.io/brain-framework/)

## What can you do with it?

**Example: Turn meeting notes into GitHub issues.** Collect Calendar event descriptions, then ask your agent:

> Turn today's meeting notes into GitHub issues in my chosen repository. Cite the source event for each issue.

BF supplies the saved notes; your agent uses `gh` to create the issues you authorize. Follow the [Calendar-to-issues example](https://fmind.github.io/brain-framework/docs/sensors/#from-meeting-notes-to-github-issues) for setup and prerequisites.

| When you need to…       | BF helps you…                                                       |
| ----------------------- | ------------------------------------------------------------------- |
| Resume a project        | Find the last verified outcome and next step.                       |
| Explain a decision      | Read the original reasoning and linked evidence.                    |
| Check work across tools | Follow explicit connections between briefs, issues and deployments. |
| Reuse what worked       | Keep lessons in notes that any editor or agent can read.            |

See the [runnable four-tool demo](https://github.com/fmind/brain-framework/tree/main/examples/context-hub): GitHub says the implementation is merged and Gcloud shows a healthy preview, but Jira still blocks launch on accessibility review. Reading all four sources explains what remains. The records are fictional; the collection and retrieval are runnable locally. The [examples index](https://github.com/fmind/brain-framework/tree/main/examples) also covers decision reviews, retrieval checks, hooks, routines, monitoring and team collaboration.

## Try it

Start with one decision you can find again. This walkthrough needs Linux or macOS and [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs the tested Python 3.14 if needed ([install details](https://fmind.github.io/brain-framework/docs/upgrades/#install)). No model or provider account is required.

```bash
uv tool install --python 3.14 brain-framework
bf init ~/brain
cd ~/brain
```

If `bf` is not on PATH, run `uv tool update-shell` and open a new shell. You can choose another empty folder instead of `~/brain`.

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

`read` returns the original Decision section; validation reports `"valid":true`. Edit the note and search again: BF notices changes automatically. You now have a decision an agent can find and cite.

The [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/) continues with a local source, a schema mapping and a link from the decision to its evidence.

## Agents

Use the same brain with [Claude Code](https://code.claude.com/docs/en/overview), [Codex](https://openai.com/codex/), [GitHub Copilot](https://github.com/features/copilot) or another agent host.

| Your agent can…       | Start with…                                                                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Run terminal commands | The `bf` CLI and the prompt below. No skill installation needed for this first task.                                                              |
| Use MCP tools         | A stdio server running `bf mcp`, with explicit executable and brain paths. Follow [MCP setup](https://fmind.github.io/brain-framework/docs/mcp/). |

After the quickstart, give your terminal agent this prompt:

> Work in ~/brain and use the bf CLI. Find why we chose a single product page. Read the source, cite its ref and tell me the project's next action.

Check that it reads the decision and next actions, cites `projects/new-website.md#decision` and identifies “Draft the product page.” as the next task.

MCP exposes two read-only tools: `search` and `read`. For ongoing use, install the optional [workflow skills](https://github.com/fmind/brain-framework/blob/main/skills/README.md): `bf-use` for retrieval, `bf-action` to track work and `bf-learn` to retain verified outcomes. See the [agent guide](https://fmind.github.io/brain-framework/docs/agents/) for setup and examples.

## How it works

**Gather → normalize → organize and connect → act → learn.** Sensors collect selected evidence; configured mappings give sources shared fields and explicit relationships. You and your agents use that context to do work, then keep the outcome and next step in the brain.

```text
brain/
├── bf.yaml      # configuration and source mappings
├── projects/    # what you're working on and why
├── concepts/    # what you've learned and can reuse
├── actions/     # work to start, resume and review
└── memories/    # evidence gathered from your sources
```

Notes are Markdown with structured metadata; each collected record is a JSON file. Open them in any editor and use Git for history. The local search cache is disposable. See the [file conventions](https://fmind.github.io/brain-framework/docs/brain/).

[Links and backlinks](https://fmind.github.io/brain-framework/docs/links/) connect decisions to evidence. Keep personal and shared context in separate [team brains](https://fmind.github.io/brain-framework/docs/team/), and search related brains together. Identities and relationship meanings are explicit; BF never guesses them from similar names.

## Bring your tools

A **sensor** is a small script that reads a CLI, API or file and prints JSON records. BF validates, maps and saves them. Start with the reviewed examples or write a script for another source:

| Source                                    | Starting point                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Local files, highlights and Git           | [Document](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py), [highlight](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.py) and [Git history](https://github.com/fmind/brain-framework/blob/main/examples/sensors/git-history.py) sensors. |
| Google Calendar and Drive                 | [Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) and [Drive folder catalog](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py) sensors using [`gws`](https://github.com/googleworkspace/cli).                         |
| GitHub, Jira, Confluence and Google Cloud | Write a sensor using [`gh`](https://cli.github.com/), [`acli`](https://developer.atlassian.com/cloud/acli/), [`gcloud`](https://cloud.google.com/sdk/gcloud) or their APIs.                                                                                                                                       |

Start with [one local source](https://fmind.github.io/brain-framework/docs/getting-started/#collect-your-first-source); use [schema mappings](https://fmind.github.io/brain-framework/docs/schema/#shared-fields-and-sensor-mappings) to connect sources. Provider tools handle authentication; your script selects what to collect.

After reviewing your sensors, run `bf watch` to keep sources refreshed and see recent changes. See [Watch and schedule updates](https://fmind.github.io/brain-framework/docs/schedule/) for timing, desktop alerts and optional native schedules.

<a href="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/watch.svg">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/watch.svg"
    alt="Watch dashboard for the fictional offline demo: five sensors sorted by state, with a failed sensor's details beside their last success, next due time, item count and record changes."
    width="720"
  >
</a>

The dashboard above shows the fictional [offline terminal demo](https://github.com/fmind/brain-framework/tree/main/examples/watch), which runs without provider accounts.

## Fit and limits

BF fits projects where decisions and evidence need to survive a chat session. Start with one recurring question and the sources needed to answer it.

- **Your files, your control.** BF sends no telemetry or personal data to us. A connected cloud agent may send what it reads to its provider; see [privacy and security](https://fmind.github.io/brain-framework/docs/privacy/).
- **Offline retrieval.** Search and read never contact providers or run sensors. Refresh sources explicitly when you need newer evidence; incomplete retrieval is reported.
- **People and agents supply the judgment.** BF uses word search and explicit identities, runs no model and does not learn automatically from conversations. Keep notes current as work changes.

## Next steps

- [Browse the commands](https://fmind.github.io/brain-framework/docs/commands/) for home pages, tasks, freshness and retrieval checks.
- [Share a team brain](https://fmind.github.io/brain-framework/docs/team/) or [run a pilot](https://fmind.github.io/brain-framework/docs/pilot/) on your own work.
- [Report an issue](https://github.com/fmind/brain-framework/issues) or [contribute](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md).

[MIT licensed](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
