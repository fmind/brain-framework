![Brain Framework Iris logo: an exposed brain in an opening shell above twin aperture eyes](../assets/brain-framework.svg){ width="96" }

# Brain Framework 🧠

**🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies.**

Brain Framework helps you and your AI agents turn scattered information into connected knowledge and informed action. Gather evidence from your tools, normalize it into shared records, connect it to projects and concepts, then retrieve the context to decide what to do next. Your brain lives in plain files you own and stays useful across sessions, editors and agent hosts.

**Your knowledge, under your control.** Keep your brain in plain files on your own machine or infrastructure. Choose what your sensors collect, inspect and edit what is stored, and search it offline without an account or model provider. You decide where to store it and whom to share it with.

Connecting a cloud AI agent may send retrieved content to its provider. Brain Framework's retrieval stays local; your agent's privacy depends on how you configure it. See [privacy and security](privacy.md) for the boundaries.

Markdown notes hold decisions and reusable knowledge; JSON Lines records hold what optional sensors gathered from selected sources. Explicit links and identities form a knowledge graph with evidence for each relationship. Brain Framework retrieves this context offline and returns readable refs to the original files. You or your agent use it to perform authorized work, record outcomes and update the notes.

Start with one decision worth remembering. Search it, read its source, and keep the note current as the project changes. Sensors are optional: a shared repository of useful notes is already a working knowledge brain.

## A brain for your work

Sensors act like senses, bringing in selected observations and extracting them into a common record format. Schema mappings normalize provider fields into a shared vocabulary. Memories retain that evidence; concepts hold the reusable knowledge you and your agents distill from it. Projects give work a focus, actions hold its context and next step, and routines prepare repeatable reviews.

**Gather → normalize → organize and connect → act → learn.** For example, find a release decision, read its linked evidence and dependencies, use that context to carry out an authorized checklist, then record the outcome and update the lesson for next time. You and your agents supply judgment; Brain Framework keeps the evidence and working context accessible.

## A knowledge graph you can inspect

Ordinary Markdown links connect notes and sources. BF addresses such as `bf://knowledge/projects/archive.md#decision` name knowledge across brains; a declared `?rel=depends-on` role expresses a typed relationship. Sensor mappings add relationships from collected records. Backlinks and claims preserve the section or record behind every connection. The graph is rebuilt from files, and aliases require explicit declarations. See [links and schema](schema.md#bf-links).

## Connect the tools you already use

Sensors make integrations open-ended: a script can turn data from a CLI, API or file export into searchable memories. Bring in GitHub issues, Jira work items, Airtable records or evidence from your own systems by writing a sensor. The repository provides four examples to adapt: local Git history, local documents, Google Calendar and Drive folders. See [sensors](sensors.md#any-source-you-can-script) for the contract and integration boundaries.

## Small parts, ordinary tools

Follow the Unix philosophy: keep knowledge in files and compose focused utilities. Use your editor for notes, Git for review, provider CLIs for authentication and a native timer for collection. Sensors print JSON records, routines print Markdown actions, and CLI commands return JSON for scripts and agents. Brain Framework contributes one command, a rebuildable SQLite cache, browsable pages and two read-only MCP tools. It requires no model, hosted database or background server. Search matches words and explicit identities, optionally within a folder, a period or an identity; it does not generate answers or automatically learn from conversations.

These docs describe Brain Framework 13.0.0. Read the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md) for release changes. Check `bf --version` and follow the [installation guide](getting-started.md) to update an older copy.

## Start with the question you need to answer

| You want to…                               | Start here                                                                                                    |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| Remember why a decision was made           | [Write and retrieve your first decision](getting-started.md#save-a-decision).                                 |
| Give a teammate enough context to continue | [Set up a team brain](team.md) and [check its answers](getting-started.md#check-the-answers-your-team-needs). |
| See what needs attention or changed        | [Read the home page, a period or a source](search.md#pages).                                                  |
| Find a decision or an exact source         | [Search by words or identity](search.md#search).                                                              |
| Bring recurring evidence into the brain    | [Add a scoped sensor](sensors.md).                                                                            |
| Let an agent use the same knowledge        | [Install a workflow skill](getting-started.md#give-agents-access) or connect the [MCP tools](mcp.md).         |
| Resume work and keep decisions explainable | [Follow the agent workflows](agents.md) for actions, evidence captures and dependency review.                 |

The [brain layout](brain.md), [command reference](commands.md), [configuration schema](schema.md) and [security model](privacy.md) cover the details when you need them.
