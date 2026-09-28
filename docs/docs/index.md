---
description: Save decisions and selected evidence in plain files, then find and reuse their context with Brain Framework.
---

![Brain Framework Iris logo](../assets/brain-framework.svg){ width="256" height="256" }

# Brain Framework 🧠

**🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies.**

Keep selected evidence, decisions and next steps in a folder you can inspect and edit. Brain Framework (BF) makes those files searchable and connects them through explicit links. You and your agents use the same evidence; no BF account or model is required.

**Start with [Getting started](getting-started.md): save a decision, find its reason and check the result.** You need Linux or macOS, a terminal and a text editor. Collection and agent setup can wait until you need them.

## See what a brain gives you

In the walkthrough, you save a fictional New website project. Later, you can recover why the team chose a single product page:

```bash
cd ~/brain
bf search "visitors clear explanation"
bf read projects/new-website.md#decision
```

The exact read returns the saved reason:

> Start with a single product page because visitors need a clear explanation before signing up.

`bf read projects` also shows the next task: **Draft the product page.** An agent can read the same decision, cite it and help with the work you authorize.

## Follow the work from evidence to outcome

**Gather → normalize → organize and connect → act → learn.**

| Step                 | In the website example                                          |
| -------------------- | --------------------------------------------------------------- |
| Gather               | A small sensor script reads a selected brief.                   |
| Normalize            | BF maps its fields and saves a record with a stable address.    |
| Organize and connect | The project links its decision to that record.                  |
| Act                  | You review the page and save the result in an action note.      |
| Learn                | You update the project and retain a useful lesson as a concept. |

Start with notes; add sources and recurring reviews as the work needs them. BF stores and retrieves evidence. People and agents interpret it and decide what to do.

## Choose a guide

You do not need to read the site in order. Choose the path that matches your task:

| Need                   | Start here                                                                                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Learn by doing         | [Getting started](getting-started.md), then the fictional [four-tool walkthrough](context-hub.md).                 |
| Understand the model   | [Core concepts](concepts.md); look up unfamiliar terms in the [glossary](glossary.md).                             |
| Use it each day        | [Write notes](brain.md), [find evidence](search.md), [link knowledge](links.md) and [check your brain](checks.md). |
| Bring in other sources | [Add a sensor](sensors.md), [collect highlights](highlights.md), then [schedule updates](schedule.md).             |
| Work with others       | [Connect an agent](agents.md) or [set up a team brain](team.md).                                                   |
| Look up exact behavior | [Commands](commands.md), [configuration](configuration.md) and the [retrieval contract](retrieval.md).             |
| Resolve a problem      | [Troubleshooting](troubleshooting.md) or [install and update](upgrades.md).                                        |

## Know the boundaries

Notes are Markdown; collected records are JSON. A disposable SQLite cache makes them searchable. Search matches words and explicit identities, without embeddings or model calls. Links and schema mappings establish connections; BF does not guess identities from similar names.

You choose what to collect and share. Sensors run only when you collect, update or watch a brain, or through your schedules. A cloud agent may send retrieved evidence to its provider. Read [Privacy and security](privacy.md) before connecting sensitive sources.

These docs follow the repository's current code. Check `bf --version` and `bf COMMAND --help` if an option differs from your installation; see [version differences](upgrades.md#match-the-docs-to-your-version).
