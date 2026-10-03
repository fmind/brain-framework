---
icon: lucide/house
description: Keep evidence, decisions and next steps in plain files that you and your agents search, read and connect.
---

![Brain Framework Iris logo](../assets/brain-framework.svg){ width="256" height="256" }

# Brain Framework 🧠

**🧠 Brain Framework: from information to informed actions.**

Keep selected evidence, decisions and next steps in a folder you can inspect and edit. Brain Framework (BF) makes those files searchable and connects them through explicit links. You and your agents use the same evidence; no BF account or model is required.

**Start with [Getting started](getting-started.md): save a decision, find its reason and check the result.** You need Linux or macOS, a terminal and a text editor.

## See what a brain gives you

In the walkthrough, you save a fictional New website project. Later, you recover why the team chose a single product page:

```bash
cd ~/brain
bf search "visitors clear explanation"
bf read projects/new-website.md#decision
```

The exact read returns the saved reason:

> Start with a single product page because visitors need a clear explanation before signing up.

`bf read projects` also shows the next task: **Draft the product page.** An agent can read the same decision, cite it and help with the work you authorize.

## Follow the work from evidence to outcome

| Step                 | In the website example                                                                             |
| -------------------- | -------------------------------------------------------------------------------------------------- |
| Gather               | A small sensor reads a selected brief.                                                             |
| Normalize            | BF maps its fields and saves a record with a stable ref.                                           |
| Organize and connect | The project links its decision to that record.                                                     |
| Act                  | You review the page and record the result in an action.                                            |
| Learn                | When the brief changes, BF flags the project; you update it and keep a useful lesson as a concept. |

Start with notes; add sources and routines as the work needs them. The [four-tool walkthrough](context-hub.md) runs the whole loop on fictional Workspace, Jira, GitHub and Gcloud evidence.

## Choose a guide

| Need                   | Start here                                                                                                                                                                               |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Learn by doing         | [Getting started](getting-started.md), then the [four-tool walkthrough](context-hub.md).                                                                                                 |
| Understand the model   | [Core concepts](concepts.md), including the glossary and what BF does not do.                                                                                                            |
| Use it each day        | [Write notes](brain.md), [search and read](search.md), [link knowledge](links.md) and [check your brain](checks.md).                                                                     |
| Bring in other sources | [Add a sensor](sensors.md), [run routines](routines.md), then [watch and schedule](schedule.md).                                                                                         |
| Work with others       | [Use an agent](agents.md), [connect one with MCP](mcp.md) or [set up a team brain](team.md).                                                                                             |
| Look up exact behavior | [Commands](commands.md), [configuration](configuration.md), [file formats](schema.md), [link rules](link-reference.md), the [retrieval reference](retrieval.md) and [limits](limits.md). |
| Resolve a problem      | [Troubleshooting](troubleshooting.md), [install and update](upgrades.md) or the [changelog](../changelog.md).                                                                            |

## Know the boundaries

Notes are Markdown; collected records are JSON. A disposable SQLite cache makes them searchable by words and by exact names, such as a repository address, without any AI model. You choose what to collect and share, and programs run only when you ask. A cloud agent may send what it reads to its provider: read [Privacy and security](privacy.md) before connecting sensitive sources.

These docs follow the repository's current code. Check `bf --version` and `bf COMMAND --help` if an option differs; see [version differences](upgrades.md#match-the-docs-to-your-version).

Agents can read these pages as Markdown: [llms.txt](https://fmind.github.io/brain-framework/llms.txt) lists every guide with a summary, and [llms-full.txt](https://fmind.github.io/brain-framework/llms-full.txt) holds their full text. In a clone, the same guides are the Markdown files in `docs/docs/`.
