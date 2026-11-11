---
icon: lucide/house
description: Keep evidence, decisions and next steps in plain files that you and your agents search, read and connect.
---

![Brain Framework logo](../assets/brain-framework.svg){ width="256" height="256" }

# Brain Framework 🧠

**🧠 Brain Framework: from information to informed actions.**

Brain Framework (BF) is a local-first brain for you and your AI agents. It collects evidence from your tools into plain files you own, connects them through an ontology you declare, lets any agent harness search and read offline, and wakes your harness when declarative `when:` rules or reviewed gates detect changes. No BF account or built-in model is required.

**Start with [Getting started](getting-started.md): save a decision, search its reason and verify the result** on Linux or macOS (or WSL2 on Windows).

## Follow the work from evidence to outcome

| Step                 | In the website example                                                                         |
| -------------------- | ---------------------------------------------------------------------------------------------- |
| Gather               | A sensor collects a selected brief into `memories/`.                                           |
| Normalize            | BF maps provider attributes into shared `fields:` with a stable `SOURCE:ID` ref.               |
| Organize and connect | The project note links its decision to that ref.                                               |
| Act                  | You or an agent track the session in `actions/` and settle it when done.                       |
| Learn                | `bf read review` flags `newer_evidence`; you reconcile the project and promote reusable rules. |

Run the full loop across four fictional tools in the [four-tool walkthrough](context-hub.md).

## Let your agent come to you

An [assistant](prompts.md) checks your brain offline and starts your harness only when `when:` matches:

```yaml
assistants:
  triage:
    prompt: prompts/triage.md
    command:
      [
        claude,
        --print,
        --output-format,
        json,
        --json-schema,
        "{{schema}}",
        --tools,
        "Read,Grep,Glob,Bash",
        --allowedTools,
        "Bash(bf read *),Bash(bf search *)",
        --permission-mode,
        dontAsk,
      ]
    reply: { path: /structured_output }
    propose: true
    post: desktop
    schedules:
      mail: { every: 600, when: { sources: [mail] } }
```

While `bf watch` runs, `when:` checks SQLite every 10 minutes without spawning a subprocess until new `mail` records arrive, posts replies to your [channel](channels.md) and writes validated `status: draft` `ACTION.md` proposals when `propose: true` ([keep prompts safe](prompts.md#keep-prompts-safe), [propose, then act](prompts.md#propose-then-act)).

## Choose a guide

| Need                   | Start here                                                                                                                                                                     |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Learn by doing         | [Getting started](getting-started.md), then the [four-tool walkthrough](context-hub.md).                                                                                       |
| Understand the model   | [Core concepts](concepts.md), including boundaries and what stays authoritative.                                                                                               |
| Use it each day        | [Write notes](brain.md), [search and read](search.md), [link knowledge](links.md) and [check your brain](checks.md).                                                           |
| Collect and automate   | [Add a sensor](sensors.md), [run routines](routines.md), [assistants](prompts.md), [channels](channels.md) and [watch and schedule](schedule.md).                              |
| Work with others       | [Agent workflows](agents.md), [MCP](mcp.md), [team setup](team.md) and [privacy and security](privacy.md).                                                                     |
| Look up exact behavior | [Commands](commands.md), [configuration](configuration.md), [notes and fields](schema.md), [link rules](link-reference.md), [retrieval](retrieval.md) and [limits](limits.md). |
| Resolve or upgrade     | [Troubleshooting](troubleshooting.md), [install and update](upgrades.md) and the [changelog](../changelog.md).                                                                 |

Check `bf --version` and `bf COMMAND --help` if an option differs from these docs ([version differences](upgrades.md#match-the-docs-to-your-version)); agents can fetch the site index via [`llms.txt`](agents.md#choose-how-your-agent-connects).
