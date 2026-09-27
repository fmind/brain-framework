![Brain Framework Iris logo: an exposed brain in an opening shell above twin aperture eyes](../assets/brain-framework.svg){ width="96" }

# Brain Framework 🧠

Keep decisions, evidence and next steps in an ordinary folder. You and your agents can search it, read the source and continue work in a later session.

**🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies.**

## See what a brain gives you

In the [getting-started example](getting-started.md), you save a New website project note, then ask why the team chose a single product page:

```bash
bf search "visitors clear explanation" --brain ~/brain
bf read projects/new-website.md#decision --brain ~/brain
```

The read returns the saved reason:

> Start with a single product page because visitors need a clear explanation before signing up.

Run `bf read projects --brain ~/brain` to find its next task: **Draft the product page.** Your agent can use the same commands, cite the decision and help with the work you authorize.

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

| You want to…                             | Start here                                                                                |
| ---------------------------------------- | ----------------------------------------------------------------------------------------- |
| Try it now                               | [Getting started](getting-started.md): create `~/brain`, save a decision and retrieve it. |
| Understand the vocabulary                | [Core concepts](concepts.md), then the [glossary](glossary.md) for individual terms.      |
| Write projects, concepts and actions     | [Files and notes](brain.md): copyable OKF Markdown examples.                              |
| Find something or see what changed       | [Search and read](search.md).                                                             |
| Connect a decision to its evidence       | [Linking knowledge](links.md).                                                            |
| Keep important answers findable          | [Check your brain](checks.md): questions and expected refs.                               |
| Bring in selected documents or tool data | [Sensors](sensors.md), starting with one local file.                                      |
| Prepare a recurring review               | [Routines](routines.md), then [Schedule updates](schedule.md).                            |
| Work with agents or teammates            | [Agent workflows](agents.md), [MCP setup](mcp.md) or [Team brains](team.md).              |

## Know the boundaries

BF runs on Linux and macOS. It stores Markdown notes and JSON Lines records; a disposable SQLite cache makes them searchable. Retrieval works offline without an account or model. Search matches words and explicit identities, without embeddings.

You choose what to collect and who can read the files. Sensors run only through collection commands or your schedules. A cloud agent may send the evidence it reads to its model provider. Read [Privacy and security](privacy.md) before connecting private sources or sharing a brain.

For exact options and limits, use [Commands](commands.md), [Configuration](configuration.md) and the [Retrieval contract](retrieval.md).
