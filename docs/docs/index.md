![Brain Framework Iris logo: an exposed brain in an opening shell above twin aperture eyes](../assets/brain-framework.svg){ width="96" }

# Brain Framework 🧠

Centralize selected information and work context from your tools in an ordinary folder. Collect it once, map it into a shared schema and let you and your agents reason across sources before making more tool calls. Add sources with small scripts using the CLIs, APIs or files already available to you.

**🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies.**

## See what a brain gives you

**“Is the website ready to launch?”** spans a Google Workspace brief, a Jira review, a GitHub implementation and a Gcloud deployment. The advanced [terminal-agent walkthrough](context-hub.md) collects fictional records from all four and links them to one project. A healthy preview and merged code still leave Jira's accessibility blocker open: the next action comes from reading the evidence together.

The mappings run automatically at ingestion: BF validates shared fields and creates declared relationships while retaining source refs. You configure the ontology and identity mappings; BF never guesses identity from similar names. Subsequent searches and reads reuse the saved evidence offline, with freshness and completeness visible.

In the [getting-started example](getting-started.md), you save a New website project note, then ask why the team chose a single product page:

```bash
cd ~/brain
bf search "visitors clear explanation"
bf read projects/new-website.md#decision
```

The read returns the saved reason:

> Start with a single product page because visitors need a clear explanation before signing up.

Run `bf read projects` to find its next task: **Draft the product page.** Your agent can use the same commands, cite the decision and help with the work you authorize.

## Follow the work from evidence to outcome

**Gather → normalize → organize and connect → act → learn.**

| Step                 | In the website example                                          |
| -------------------- | --------------------------------------------------------------- |
| Gather               | A sensor reads a selected product brief.                        |
| Normalize            | BF saves it as a record with a stable ref.                      |
| Organize and connect | The project links its decision to that record.                  |
| Act                  | You review the page; an action keeps the checks and next step.  |
| Learn                | You update the project and retain a useful lesson as a concept. |

Start with the note. Add sensors when you need evidence from other tools, and routines when a review is worth repeating. People and agents judge the evidence; BF does not generate answers or learn automatically from conversations.

## Choose a guide

| You want to…                             | Start here                                                                                           |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Try it now                               | [Getting started](getting-started.md): create a brain, save a decision and collect one local source. |
| Ask one question across tools            | [Advanced multi-tool example](context-hub.md): four sources and a terminal-agent prompt.             |
| Introduce BF to a team                   | [Team setup](team.md): a private repository, local collection and shared decisions.                  |
| Understand the vocabulary                | [Core concepts](concepts.md), then the [glossary](glossary.md) for individual terms.                 |
| Write projects, concepts and actions     | [Files and notes](brain.md): copyable OKF Markdown examples.                                         |
| Find something or see what changed       | [Search and read](search.md).                                                                        |
| Connect a decision to its evidence       | [Linking knowledge](links.md).                                                                       |
| Keep important answers findable          | [Check your brain](checks.md): questions and expected refs.                                          |
| Bring in selected documents or tool data | [Sensors](sensors.md), starting with one local file.                                                 |
| Prepare a recurring review               | [Routines](routines.md), then [Schedule updates](schedule.md).                                       |
| Work with agents or teammates            | [Agent workflows](agents.md), [MCP setup](mcp.md) or [Team brains](team.md).                         |

## Know the boundaries

BF runs on Linux and macOS. It stores Markdown notes and JSON record files; a disposable SQLite cache makes them searchable. Retrieval works offline without an account or model. Search matches words and explicit identities, without embeddings.

You choose what to collect and who can read the files. Sensors run only through collection commands or your schedules. A cloud agent may send the evidence it reads to its model provider. Read [Privacy and security](privacy.md) before connecting private sources or sharing a brain.

For exact options and limits, use [Commands](commands.md), [Configuration](configuration.md) and the [Retrieval contract](retrieval.md).
