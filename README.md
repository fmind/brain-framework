<p align="center">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-framework.svg"
    alt="Brain Framework Iris logo: an exposed brain in an opening shell above twin aperture eyes"
    width="128"
    height="128"
  >
</p>

# Brain Framework 🧠

**🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies.**

[![CI](https://github.com/fmind/brain-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fmind/brain-framework/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/brain-framework?color=174EA6)](https://pypi.org/project/brain-framework/) [![Python](https://img.shields.io/pypi/pyversions/brain-framework)](https://pypi.org/project/brain-framework/) [![License: MIT](https://img.shields.io/badge/license-MIT-174EA6)](https://github.com/fmind/brain-framework/blob/main/LICENSE)

Brain Framework helps you and your AI agents **turn scattered information into connected knowledge and informed action**. Gather evidence from your tools, normalize it into shared records, connect it to projects and concepts, then retrieve the context to decide what to do next. Your brain lives in plain files you own and stays useful across sessions, editors and agent hosts.

A chat ends. A project pauses. A teammate moves on. The next person or agent should be able to find what happened, why it mattered and where to continue.

**Bring your tools. Choose your agent. Keep the knowledge.** Connect the systems you already use with small scripts, then reuse the same brain in Claude Code, Codex, GitHub Copilot or another agent with CLI or MCP access.

**Your knowledge, under your control.** Keep your brain in plain files on your own machine or infrastructure. Choose what your sensors collect, inspect and edit what is stored, and search it offline without an account or model provider. You decide where to store it and whom to share it with.

Connecting a cloud AI agent may send retrieved content to its provider. Brain Framework's retrieval stays local; your agent's privacy depends on how you configure it. See [privacy and security](https://fmind.github.io/brain-framework/docs/privacy/) for the boundaries.

[Get started](https://fmind.github.io/brain-framework/docs/getting-started/) · [Documentation](https://fmind.github.io/brain-framework/) · [Example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain) · [Connect an agent](https://fmind.github.io/brain-framework/docs/agents/)

## How it works

The everyday loop is **gather → normalize → organize and connect → act → learn**. Each part has a concrete job:

| Step                     | What happens                                                                                                         | What it enables                                                        |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| **Gather**               | Sensors collect selected evidence from APIs, CLIs and local files into **memories**.                                 | Bring a release's commits, documents and meetings into the same brain. |
| **Normalize**            | Sensors extract source data; BF validates records and maps declared fields into a shared schema.                     | Use the same author or owner identity across different sources.        |
| **Organize and connect** | **Projects** hold goals and decisions; **concepts** hold reusable knowledge. Links form an explicit knowledge graph. | Follow a decision to its evidence, dependencies and related work.      |
| **Act**                  | People and agents retrieve context; **actions** keep objectives, inputs, outputs and the next step.                  | Resume a rollout with its rationale and checklist at hand.             |
| **Learn**                | People and agents review outcomes and update notes; **routines** prepare recurring reviews.                          | Carry a release lesson into the next project's decision.               |

For example, before a release, an agent can find the rollout decision, inspect its linked evidence and dependencies, then use that context to help carry out an authorized checklist. It records the outcome in the action and updates the project or reusable lesson for next time.

Brain Framework supplies the collection, organization and retrieval tools. You and your agents supply judgment and perform the work. Start with one project note; add sensors and routines as recurring questions call for them.

## Try it

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv tool install --python 3.14 'brain-framework==13.0.0'
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
- **Collect once, reuse across workflows.** The same saved evidence can support a meeting brief, a project review and an agent's next task.
- **Review what changed.** Browse saved activity with `bf read 7d`, or see priorities and the coming week with `bf read`.
- **Learn from the outcome.** Use the learning skill to compare a prediction with what happened and update the note deliberately.

For example, `bf search "release strategy"` finds matching refs. `bf read projects/release.md#decision` reads that decision's exact text. Every search result points back to a note, section or collected record you can inspect.

## Why plain files?

Brain Framework follows the **Unix philosophy: keep knowledge in files and compose small tools with clear jobs**. Your editor writes notes, Git keeps history, sensors call provider tools, native timers trigger updates, and agents reason over retrieved context.

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

Optional `sensors/` and `routines/` hold the programs you configure. Commands return JSON on stdout and diagnostics on stderr, so ordinary scripts can use the same interface as agents. For example, with [jq](https://jqlang.org/), list the refs returned by a search:

```bash
bf search "release strategy" | jq -r '.items[].ref'
```

A sensor prints JSON records; a routine reads the brain and prints a Markdown action for review. These small process contracts let you extend your workflow without changing the framework. See the [brain layout](https://fmind.github.io/brain-framework/docs/brain/) for the full file conventions.

## Links across brains

**The knowledge graph is built from your files.** Ordinary Markdown links connect notes and source URLs; explicit identities and schema mappings connect collected records. BF builds a disposable SQLite graph for backlinks, relationships and source evidence. You can inspect the underlying files without running a graph database service.

For example, an action can link to a decision in a brain named `knowledge`:

```markdown
[Retention decision](bf://knowledge/projects/archive.md#decision)

[Depends on that decision](bf://knowledge/projects/archive.md?rel=depends-on#decision)
```

The first is a reference; the second declares a dependency using a role defined in `bf.yaml`. The syntax is ordinary Markdown; `bf://` addresses and `?rel=` roles are Brain Framework conventions. Relative file links also work for local notes, and HTTPS links retain their original targets.

Read the whole note with `bf read bf://knowledge/projects/archive.md` to see its backlinks grouped by relationship, then follow the returned refs to their evidence. Each claim retains the section or record that asserted it. An explicit alias can connect an account to a person's note; similar names alone never merge people or invent relationships. The graph exposes declared connections, without inferring chains of conclusions.

These addresses work across [selected brains](https://fmind.github.io/brain-framework/docs/schema/#across-brains) without embedding machine paths or fetching remote data. The [link and schema guide](https://fmind.github.io/brain-framework/docs/schema/#bf-links) covers identities, roles and provenance.

## Any source you can script

**APIs, CLIs and MCP servers are everywhere. Use the access they already provide.** A sensor can call `gh` for GitHub, `gws` for Google Workspace, `acli` for Jira, an HTTP API or an MCP server through a client you script. A small script turns the selected results into the brain's record format, without a framework-specific plugin or a change to Brain Framework itself. For example, you can collect:

| Source                  | What could become a memory                                        |
| ----------------------- | ----------------------------------------------------------------- |
| **GitHub**              | Issues, pull requests, reviews and release notes.                 |
| **Jira**                | Work items, their status and the discussion behind a decision.    |
| **Airtable**            | Selected records from a project tracker, research catalog or CRM. |
| **Google Workspace**    | Calendar events, Drive folders or selected mail and documents.    |
| **Local files and Git** | Notes, PDFs, Office documents and commit history.                 |
| **Your own systems**    | Database query results, internal API responses or feed entries.   |

**The integration surface is a script.** Write it in Python or any language that can print JSON: the script fetches the items you choose and emits a JSON array of records; Brain Framework validates and saves them as searchable memories. Readable exports work too.

**Your tools → sensor script → validated records and shared fields → linked, searchable memories.**

There is no fixed connector catalog to outgrow. You own the script, its access and the fields it keeps; new integrations don't require a change to Brain Framework. Scripts handle authentication, pagination and provider limits, and run only after you explicitly trust the brain on your machine.

Start from the four [reviewed sensor examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors): **local Git history, local documents, Google Calendar and Drive folders**. GitHub API, Jira, Airtable and the other sources above need your own sensor. The [sensor guide](https://fmind.github.io/brain-framework/docs/sensors/) explains the small JSON contract and how to connect a script.

## Agents

**Switch agents without rebuilding your knowledge.** Claude Code, Codex, GitHub Copilot and other agents can reuse the same notes, collected evidence and saved next steps through the CLI or MCP. Save a decision with one agent and let another pick up from it: the content belongs to your brain and stays independent of any host's chat history. Configure each host's access once, then install the skills that match your workflow:

| Skill                                                                                         | What it teaches your agent                                   |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| [bf-setup](https://github.com/fmind/brain-framework/blob/main/skills/bf-setup/SKILL.md)       | Set up a useful brain and verify agent access.               |
| [bf-scan](https://github.com/fmind/brain-framework/blob/main/skills/bf-scan/SKILL.md)         | Discover useful sources within an approved inspection scope. |
| [bf-use](https://github.com/fmind/brain-framework/blob/main/skills/bf-use/SKILL.md)           | Search, read and cite evidence before answering.             |
| [bf-learn](https://github.com/fmind/brain-framework/blob/main/skills/bf-learn/SKILL.md)       | Turn reviewed lessons and decisions into lasting notes.      |
| [bf-action](https://github.com/fmind/brain-framework/blob/main/skills/bf-action/SKILL.md)     | Start or resume an action with its context and next step.    |
| [bf-maintain](https://github.com/fmind/brain-framework/blob/main/skills/bf-maintain/SKILL.md) | Maintain collection, routines and brain health.              |

Use `bf-setup` for guided onboarding and `bf-scan` for optional, approved source discovery; `bf-maintain` implements selected integrations. Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md); skills are installed separately from the Python package. Then try: **“Search my brain for why we chose this release strategy. Read the source and cite it.”**

Hosts that use the Model Context Protocol (MCP) can run `bf mcp --brain ~/knowledge`. It exposes exactly two read-only tools: `search` and `read`. See [agent workflows](https://fmind.github.io/brain-framework/docs/agents/) and [MCP setup](https://fmind.github.io/brain-framework/docs/mcp/).

## Personal and team brains

Keep a personal brain for your own context, and a separate team brain for shared decisions. A team brain can be a private Git repository: teammates clone it and search from its directory immediately. Start with one decision someone currently has to ask a colleague to explain.

Declare related brains in `bf.yaml` to search them together. Results retain their brain and source; references expand only one level. A clone never grants permission to run its sensors. See the [team guide](https://fmind.github.io/brain-framework/docs/team/) for shared notes, collection trust and CI collection.

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
- **Sources stay visible.** Results carry readable refs and source coverage; incomplete retrieval is reported through `problems` or `stale`. Search and listing pages offer `next_offset` for continuation, and oversized exact reads return lossless JSON chunks.

Retrieved content is evidence, never instructions. Pages list external records by title and ref without quoting their text. The [privacy and security guide](https://fmind.github.io/brain-framework/docs/privacy/) explains execution boundaries and recovery.

## Fit and limits

Brain Framework fits people and teams building workflows with AI agents who want editable knowledge, traceable decisions and control over their tools. Formats can change in major releases, with manual upgrade instructions.

Search matches words and explicit identities. The framework runs no model, generates no answers and needs no embeddings; reasoning belongs to your chosen agent. It does not automatically learn from conversations or execute a plan because a note suggests it. The value depends on collecting relevant evidence, maintaining useful links and reviewing what the work taught you.

A brain is a context boundary, not an access-control system. Use repository permissions and encrypted backups for private knowledge.

## Development

Contributions are welcome. Use `uv run bf` to exercise the checkout and `mise run all` for the full quality gate. See [CONTRIBUTING.md](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md) for setup, tests and release procedures.

[MIT licensed](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
