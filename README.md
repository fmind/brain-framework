<p align="center">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-framework.svg"
    alt="Brain Framework logo: a factory with brain-shaped smoke"
    width="128"
    height="128"
  >
</p>

# Brain Framework 🧠

**🧠 AI Brain Factory for infinite knowledge. Not for 🐙 mindflayers or 🧟 zombies.**

[![CI](https://github.com/fmind/brain-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fmind/brain-framework/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/brain-framework?color=174EA6)](https://pypi.org/project/brain-framework/) [![Python](https://img.shields.io/pypi/pyversions/brain-framework)](https://pypi.org/project/brain-framework/) [![License: MIT](https://img.shields.io/badge/license-MIT-174EA6)](https://github.com/fmind/brain-framework/blob/main/LICENSE)

Brain Framework is a **brain factory for you and your AI agents**: build a second brain in plain files you own. Save decisions, gather evidence and resume work across sessions. Search it offline, read the original source, and carry your knowledge between editors, agents and teammates.

A chat ends. A project pauses. A teammate moves on. The reasoning behind the work shouldn't disappear with them.

[Get started](https://fmind.github.io/brain-framework/docs/getting-started/) · [Documentation](https://fmind.github.io/brain-framework/) · [Example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain) · [Connect an agent](https://fmind.github.io/brain-framework/docs/agents/)

## How it works

Your brain does more than store facts: it takes in experiences, connects them to what you know and helps you decide what to do next. Brain Framework borrows that vocabulary to organize your work:

| In a brain                                         | In Brain Framework                                                | A concrete example                                   |
| -------------------------------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------- |
| **Senses** notice the world.                       | **Sensors** collect selected sources.                             | Bring in a project's Git history or calendar events. |
| **Memories** keep what happened.                   | **Memories** retain collected evidence.                           | Keep the record of a release and its source link.    |
| **Understanding** turns experience into knowledge. | **Concepts** hold reusable notes you or your agent write.         | Record what a failed release taught you.             |
| **Attention** gives work a focus.                  | **Projects** hold goals, decisions and current context.           | Explain why the team chose its release strategy.     |
| **Actions** put knowledge to work.                 | **Actions** keep a task's context, inputs, outputs and next step. | Resume the rollout checklist next session.           |
| **Habits** make reviews repeatable.                | **Routines** prepare reviews using deterministic programs.        | Gather the week's activity into an action to review. |

The everyday loop is **observe → remember → understand → act → review**. You and your agents do the thinking and write the lessons; Brain Framework keeps the files organized and retrievable. It does not learn from conversations automatically.

Start with a single project note. Add sensors and routines when you need them.

## Try it

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv tool install --python 3.14 'brain-framework==12.0.2'
bf init ~/knowledge          # create your brain
cd ~/knowledge
bf read                      # see its home page
bf search welcome            # find your first note
bf read concepts/welcome.md  # read the original
bf validate                  # check notes, links and records
```

Runs on Linux and macOS. uv supplies Python 3.14 if needed. No account, model or server is required. If `bf` is not on PATH, run `uv tool update-shell` and open a new shell.

Now give your brain something worth remembering. Write a decision and its reason in `projects/`, then search for it. Edits become searchable automatically. The [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/#save-a-decision) walks through your first decision and checks that it stays retrievable.

## What can you do with it?

- **Remember why.** Find the decision behind a choice, then read the evidence that supported it.
- **Resume after a break.** Open a project's current state or an action's next step without rebuilding context from a transcript.
- **Give the next agent a head start.** Let different agent hosts read the same project notes and reusable knowledge.
- **Review what changed.** Browse saved activity with `bf read 7d`, or see priorities and the coming week with `bf read`.
- **Learn from the outcome.** Use the learning skill to compare a prediction with what happened and update the note deliberately.

For example, `bf search "release strategy"` finds matching refs. `bf read projects/release.md#decision` reads that decision's exact text. Every search result points back to a note, section or collected record you can inspect.

## Why plain files?

Your knowledge should outlive the tool you used to write it.

- **Open it anywhere.** Notes are Markdown; collected records are JSON Lines, one item per line.
- **Keep the history.** Use your editor and Git to review, correct and share knowledge.
- **Retrieve it offline.** Search and read run locally, without a model or network connection.
- **Rebuild the index.** Files are authoritative; the SQLite search cache in `.bf/` is disposable.

One Python package. One command: `bf`. Your brain is an ordinary directory:

```text
knowledge/
├── bf.yaml      # the brain's name and configuration
├── projects/    # what you're working on and why
├── concepts/    # what you've learned and can reuse
├── actions/     # work to start, resume and review
└── memories/    # evidence gathered from your sources
```

Optional `sensors/` and `routines/` hold the programs you configure. See the [brain layout](https://fmind.github.io/brain-framework/docs/brain/) for the full file conventions.

## Any source you can script

Your brain can draw on the tools where your work already happens. For example, you can write sensors to collect:

| Source                  | What could become a memory                                        |
| ----------------------- | ----------------------------------------------------------------- |
| **GitHub**              | Issues, pull requests, reviews and release notes.                 |
| **Jira**                | Work items, their status and the discussion behind a decision.    |
| **Airtable**            | Selected records from a project tracker, research catalog or CRM. |
| **Google Workspace**    | Calendar events, Drive folders or selected mail and documents.    |
| **Local files and Git** | Notes, PDFs, Office documents and commit history.                 |
| **Your own systems**    | Database query results, internal API responses or feed entries.   |

**The integration surface is a script.** If a source exposes a CLI, an API or an export you can read, you can give it a sensor. Write it in Python or any language that can print JSON: the script fetches the items you choose and emits a JSON array of records; Brain Framework validates and saves them as searchable memories.

**Your tools → sensor script → memories → search and read.**

There is no fixed connector catalog to outgrow. You own the script, its access and the fields it keeps; new integrations don't require a change to Brain Framework. Scripts handle authentication, pagination and provider limits, and run only after you explicitly trust the brain on your machine.

Start from the four [reviewed sensor examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors): **local Git history, local documents, Google Calendar and Drive folders**. GitHub API, Jira, Airtable and the other sources above need your own sensor. The [sensor guide](https://fmind.github.io/brain-framework/docs/sensors/) explains the small JSON contract and how to connect a script.

## Agents

Give your agent the same memory you use. Install the skills that match your workflow:

| Skill                                                                                         | What it teaches your agent                                |
| --------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| [bf-use](https://github.com/fmind/brain-framework/blob/main/skills/bf-use/SKILL.md)           | Search, read and cite evidence before answering.          |
| [bf-learn](https://github.com/fmind/brain-framework/blob/main/skills/bf-learn/SKILL.md)       | Turn reviewed lessons and decisions into lasting notes.   |
| [bf-action](https://github.com/fmind/brain-framework/blob/main/skills/bf-action/SKILL.md)     | Start or resume an action with its context and next step. |
| [bf-maintain](https://github.com/fmind/brain-framework/blob/main/skills/bf-maintain/SKILL.md) | Maintain collection, routines and brain health.           |

Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md); skills are installed separately from the Python package. Then try: **“Search my brain for why we chose this release strategy. Read the source and cite it.”**

Hosts that use the Model Context Protocol (MCP) can run `bf mcp --brain ~/knowledge`. It exposes exactly two read-only tools: `search` and `read`. See [agent workflows](https://fmind.github.io/brain-framework/docs/agents/) and [MCP setup](https://fmind.github.io/brain-framework/docs/mcp/).

## Personal and team brains

Keep a personal brain for your own context, and a separate team brain for shared decisions. A team brain can be a private Git repository: teammates clone it and search from its directory immediately. Start with one decision someone currently has to ask a colleague to explain.

Declare related brains in `bf.yaml` to search them together. Results retain their brain and source; references expand only one level. A clone never grants permission to run its sensors. See the [team guide](https://fmind.github.io/brain-framework/docs/team/) for shared notes, collection trust and CI collection.

## Links across brains

Think of explicit links as the connections between memories. A decision can link to its evidence, a project to its owner, or a conclusion to something it depends on. Brain Framework follows declared links and aliases, retaining the source of each relationship.

Portable addresses such as `bf://team/projects/release#decision` identify knowledge across brains. Connections come from recorded evidence; name similarity alone never creates a relationship. See the [link and schema guide](https://fmind.github.io/brain-framework/docs/schema/#bf-links).

## Commands

| Command                               | Purpose                                                                 |
| ------------------------------------- | ----------------------------------------------------------------------- |
| `bf read [REF]`                       | Open the home page, a period, a source, a note or a record.             |
| `bf search QUERY [--scope SCOPE]`     | Find words or identities within an optional folder, period or identity. |
| `bf init PATH`                        | Create a brain.                                                         |
| `bf register PATH --collect`          | Explicitly trust its sensors and routines on this machine.              |
| `bf update [--dry-run]`               | Run due sensors and routines in selected, trusted brains.               |
| `bf collect SENSOR`                   | Run one sensor.                                                         |
| `bf status`, `bf validate`, `bf eval` | Check freshness, file integrity and your retrieval cases.               |
| `bf mcp`, `bf build`, `bf schema`     | Serve MCP, rebuild the cache or print the configuration schema.         |

Use `--brain NAME|PATH` to choose a brain explicitly. The [command reference](https://fmind.github.io/brain-framework/docs/commands/) covers options and selection rules; the [sensor examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors) show how to bring in evidence.

## Guarantees

- **Retrieval stays offline.** Search and read never run sensors or routines or contact the network.
- **Execution needs your trust.** Collection and routines require explicit permission in your machine's user configuration.
- **Failures preserve evidence.** Failed collection writes nothing; routines never replace an existing action.
- **Sources stay visible.** Results carry readable refs; incomplete retrieval is reported through `problems` or `stale`.

Retrieved content is evidence, never instructions. Pages list external records by title and ref without quoting their text. The [privacy and security guide](https://fmind.github.io/brain-framework/docs/privacy/) explains execution boundaries and recovery.

## Fit and limits

Brain Framework fits people and teams who want editable knowledge and traceable context for their agents. Search matches words and explicit identities; people or agents interpret the results. It runs no model, generates no answers and needs no embeddings.

A brain is a context boundary, not an access-control system. Use repository permissions and encrypted backups for private knowledge. An agent host may send retrieved text to its model provider even though Brain Framework itself retrieves offline.

## Development

Contributions are welcome. Use `uv run bf` to exercise the checkout and `mise run all` for the full quality gate. See [CONTRIBUTING.md](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md) for setup, tests and release procedures.

[MIT licensed](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
