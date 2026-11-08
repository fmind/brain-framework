---
icon: lucide/house
description: Keep evidence, decisions and next steps in plain files that you and your agents search, read and connect.
---

![Brain Framework logo](../assets/brain-framework.svg){ width="256" height="256" }

# Brain Framework 🧠

**🧠 Brain Framework: from information to informed actions.**

Brain Framework (BF) is a local-first brain for you and your AI agents. It gathers evidence from your tools into plain files you own, connects it through an ontology you declare, lets any agent harness search it offline and can wake that harness when a gate you review finds an event. No BF account or model is required.

**Start with [Getting started](getting-started.md): save a decision, find its reason and check the result.** You need Linux or macOS, a terminal and a text editor.

## Follow the work from evidence to outcome

| Step                 | In the website example                                                                             |
| -------------------- | -------------------------------------------------------------------------------------------------- |
| Gather               | A small sensor reads a selected brief.                                                             |
| Normalize            | BF maps its fields and saves a record with a stable ref.                                           |
| Organize and connect | The project links its decision to that record.                                                     |
| Act                  | You review the page and record the result in an action.                                            |
| Learn                | When the brief changes, BF flags the project; you update it and keep a useful lesson as a concept. |

Start with notes; add sources, routines and assistants as the work needs them. The [four-tool walkthrough](context-hub.md) runs the whole loop on fictional Workspace, Jira, GitHub and Gcloud evidence.

## Let your agent come to you

An [assistant](prompts.md) can watch what your sensors collect and speak first:

```yaml
assistants:
  triage:
    prompt: prompts/triage.md
    command: [claude, --print, --output-format, json, --json-schema, "{{schema}}"] # your harness and its permissions
    reply: { path: /structured_output } # {say, wake}
    post: desktop # a channel you declare
    wake: { min: 600, max: 86400 }
    schedules:
      mail: { every: 600, gate: { command: [python3, routines/records-gate.py, mail] } }
```

While `bf watch` runs, a [gate](https://github.com/fmind/brain-framework/blob/main/examples/routines/records-gate.py) you review checks the collected records every 10 minutes. Each check starts your harness only when the gate finds events. The harness speaks or stays silent and may ask for one follow-up wake. BF posts its answer to your channel and records each run. Mail text reaches your harness, so [limit its tools](prompts.md#keep-prompts-safe), or let a read-only assistant [propose and another act](prompts.md#propose-then-act).

## Choose a guide

| Need                   | Start here                                                                                                                                                                               |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Learn by doing         | [Getting started](getting-started.md), then the [four-tool walkthrough](context-hub.md).                                                                                                 |
| Understand the model   | [Core concepts](concepts.md), including the glossary and what BF does not do.                                                                                                            |
| Use it each day        | [Write notes](brain.md), [search and read](search.md), [link knowledge](links.md) and [check your brain](checks.md).                                                                     |
| Collect and automate   | [Add a sensor](sensors.md), [run routines](routines.md) and [assistants](prompts.md), then [watch and schedule](schedule.md).                                                            |
| Work with others       | [Use an agent](agents.md), [connect one with MCP](mcp.md) or [set up a team brain](team.md).                                                                                             |
| Look up exact behavior | [Commands](commands.md), [configuration](configuration.md), [file formats](schema.md), [link rules](link-reference.md), the [retrieval reference](retrieval.md) and [limits](limits.md). |
| Resolve a problem      | [Troubleshooting](troubleshooting.md), [install and update](upgrades.md) or the [changelog](../changelog.md).                                                                            |

## Know the boundaries

Notes are Markdown; collected records are JSON. A disposable SQLite cache makes them searchable by words and by exact names, such as a repository address, without any AI model. You choose what to collect and share, and programs run only when you ask. A cloud agent may send what it reads to its provider: read [Privacy and security](privacy.md) before connecting sensitive sources.

These docs follow the repository's current code. Check `bf --version` and `bf COMMAND --help` if an option differs; see [version differences](upgrades.md#match-the-docs-to-your-version). Agents can read them as [Markdown](agents.md#choose-how-your-agent-connects).
