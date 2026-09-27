<p align="center">
  <img
    src="https://raw.githubusercontent.com/fmind/brain-framework/main/docs/assets/brain-framework.svg"
    alt="Brain Framework: an exposed brain in an opening shell above twin aperture eyes"
    width="128"
    height="128"
  >
</p>

# Brain Framework 🧠

**Give your AI agents project context they can find, explain and carry into the next session.**

[![CI](https://github.com/fmind/brain-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fmind/brain-framework/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/brain-framework?color=174EA6)](https://pypi.org/project/brain-framework/) [![Python](https://img.shields.io/pypi/pyversions/brain-framework)](https://pypi.org/project/brain-framework/) [![License: MIT](https://img.shields.io/badge/license-MIT-174EA6)](https://github.com/fmind/brain-framework/blob/main/LICENSE)

Brain Framework (BF) gives you and your agents a **brain: an ordinary folder of Markdown notes and collected records**. Save a decision, link its evidence and record the next step. Any agent with command-line or Model Context Protocol (MCP) access can find and read it in a later session.

Start a session with **“Why did we choose this approach, and what should happen next?”** Save the answer and its evidence once; let the next agent read it instead of reconstructing the reasoning from chat history.

🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies.

[Try it](https://github.com/fmind/brain-framework#try-it) · [Connect an agent](https://github.com/fmind/brain-framework#agents) · [Documentation](https://fmind.github.io/brain-framework/) · [Example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain)

## What can you do with it?

| Ask…                               | The brain provides…                                 |
| ---------------------------------- | --------------------------------------------------- |
| “Why did we choose this approach?” | A project decision and the evidence linked to it.   |
| “Where did we stop?”               | An action's last verified state and next step.      |
| “What can we reuse?”               | A concept with its supporting sources and limits.   |
| “What changed this week?”          | Saved notes and collected records from that period. |

BF collects, validates and retrieves evidence. **You and your agents decide what it means and what to do.**

## Try it

Ask your agent to follow these steps, or try them yourself on Linux or macOS. Install [uv](https://docs.astral.sh/uv/getting-started/installation/); it supplies a compatible Python version if needed.

```bash
uv tool install brain-framework
bf init ~/brain
cd ~/brain
```

If `bf` is not on PATH, run `uv tool update-shell` and open a new shell. `~/brain` is the recommended starting location; choose another folder if you prefer, such as `~/team-brain` or `~/brains/default`. See [folder and naming choices](https://fmind.github.io/brain-framework/docs/getting-started/#choose-a-location) before changing the examples.

Create `projects/new-website.md` yourself or ask your agent to save this fictional decision. Use today's date for `updated`:

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

Find the reason, read the source, then check the brain:

```bash
bf search "visitors clear explanation"
bf read projects/new-website.md#decision
bf validate
```

Search returns `projects/new-website.md#decision` in `items`. Reading that ref returns the original section; these are selected fields from the JSON replies:

```json
{ "ref": "projects/new-website.md#decision", "title": "New website — Decision" }
```

```json
{
  "text": "## Decision\n\nStart with a single product page because visitors need a clear explanation before signing up.\n\n"
}
```

Validation should report `"valid":true`. Edit the note and search again: BF notices changes automatically. You now have a decision an agent can find, explain and cite.

Use `bf read` for the brain's home page or `bf read projects` for project next steps. From another directory, add `--brain ~/brain`. The [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/) walks through the same decision in more detail.

## Agents

**Give your agent access to the brain, then ask it to use the evidence.** [Claude Code](https://code.claude.com/docs/en/overview), [Codex](https://openai.com/codex/), [GitHub Copilot](https://github.com/features/copilot) and other hosts can use one of two routes:

| Your agent can…       | Connect it with…                                                                                                                                                     |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Run terminal commands | The `bf` CLI and the [bf-use skill](https://github.com/fmind/brain-framework/blob/main/skills/bf-use/SKILL.md), which teaches search, exact reads and citations.     |
| Use MCP tools         | A stdio server running `bf mcp --brain ~/brain`. Follow [MCP setup](https://fmind.github.io/brain-framework/docs/mcp/) for host configuration and connection checks. |

MCP exposes exactly two read-only tools: `search` and `read`. Set the host's executable and brain paths explicitly. Skills are installed separately from the Python package; use [skills matching your release](https://github.com/fmind/brain-framework/blob/main/skills/README.md) and your host's discovery directory.

After connecting, try this prompt against the decision above:

> Search my brain for why we chose a single product page. Read the matching source and cite its ref. Tell me the next action recorded for that project.

Check that the agent reads `projects/new-website.md#decision` and the project's next actions, explains that visitors need a clear explanation before signing up, and cites the source. A plausible answer alone does not verify the connection.

Start with `bf-use` for retrieval. Add `bf-learn` to keep notes current, `bf-action` to track a session, or `bf-setup` for guided onboarding. [All six workflow skills](https://github.com/fmind/brain-framework/blob/main/skills/README.md) are optional, separately installed packages.

For example, after drafting the page, ask:

> Update the New website project with what we delivered and the next task. Keep the original reason for the decision. Record only what we actually checked.

The [agent guide](https://fmind.github.io/brain-framework/docs/agents/) shows setup and the work → review → update cycle.

## How it works

The goal is the whole loop: **gather → normalize → organize and connect → act → learn**.

| Step                     | In the New website example…                                                                        |
| ------------------------ | -------------------------------------------------------------------------------------------------- |
| **Gather**               | A sensor collects the selected product brief as evidence.                                          |
| **Normalize**            | BF validates the record and maps declared fields into a shared schema.                             |
| **Organize and connect** | The project records the single-page decision and links to its brief and reusable lessons.          |
| **Act**                  | An agent reads the decision and helps draft the page. A review action keeps checks and next steps. |
| **Learn**                | You review the page, update the project and retain what you learned about explaining the product.  |

Start with one useful note. Add sources when you have a recurring question they can answer; add routines when a review is worth repeating. Learning happens through deliberate updates by you or your agent.

## Bring your tools

<span id="1-gather-and-shape-the-evidence"></span>
<span id="2-declare-meaning-and-map-fields"></span>
<span id="3-retrieve-the-value"></span>

A **sensor** is a script that gathers selected evidence and prints JSON records. Use the tools you already have:

| Source                          | Starting point                                                                                                                                                                                                                                                                              |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Local Git history and documents | Adapt the reviewed [Git](https://github.com/fmind/brain-framework/blob/main/examples/sensors/git-history.py) or [document](https://github.com/fmind/brain-framework/blob/main/examples/sensors/local-documents.py) sensor.                                                                  |
| Google Calendar and Drive       | Adapt the reviewed [Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) or [Drive](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py) sensor using [`gws`](https://github.com/googleworkspace/cli). |
| GitHub                          | Write a sensor using [`gh`](https://cli.github.com/) or the API.                                                                                                                                                                                                                            |
| Jira and Confluence             | Write a sensor using [`acli`](https://developer.atlassian.com/cloud/acli/) or the APIs.                                                                                                                                                                                                     |

The [first-sensor walkthrough](https://fmind.github.io/brain-framework/docs/sensors/#your-first-sensor) collects one local product brief. After completing it, read the saved evidence:

```bash
bf read local-documents:website-demo/brief.txt --brain ~/brain
```

The reply contains the brief's text and source location. Link that ref from the project decision. Scripts own authentication and provider limits; BF validates and saves their records. [Schema mappings](https://fmind.github.io/brain-framework/docs/schema/#shared-fields-and-sensor-mappings) let different sources use the same declared field meanings.

## Why plain files?

Open the brain in any editor. Use Git for history, provider tools for collection and your chosen agent for reasoning. BF stays one package and one command:

```text
brain/
├── bf.yaml      # the brain's name and configuration
├── projects/    # what you're working on and why
├── concepts/    # what you've learned and can reuse
├── actions/     # work to start, resume and review
└── memories/    # evidence gathered from your sources
```

Projects, concepts and action entry notes use Open Knowledge Format (OKF): Markdown with YAML metadata such as `type`, `status` and `sources`. Collected records are JSON Lines. Files are authoritative, and the SQLite search cache in `.bf/` is disposable. Optional `sensors/` and `routines/` hold your configured programs. See the [brain layout](https://fmind.github.io/brain-framework/docs/brain/) for file conventions.

## Links across brains

Link a decision to its source with ordinary Markdown links, or use a portable BF address:

```markdown
[New website decision](bf://brain/projects/new-website.md#decision)
```

Read the target to see its **backlinks**: the notes and records that point to it. Declared relationships such as `depends-on` also retain the section that made the claim. Follow [Linking knowledge](https://fmind.github.io/brain-framework/docs/links/) for a working example.

## Personal and team brains

Keep personal context in `~/brain` and shared decisions in a separate private repository, such as `~/team-brain`. Teammates can clone and search it. Declare related brains in `bf.yaml` to search them together; reading a clone never runs its sensors. See [team brains](https://fmind.github.io/brain-framework/docs/team/).

## Commands

Commands return JSON on stdout and diagnostics on stderr, so scripts and agents use the same interface.

| Command                               | Purpose                                                     |
| ------------------------------------- | ----------------------------------------------------------- |
| `bf read [REF]`                       | Open the home page, a period, a note or a record.           |
| `bf search QUERY [--scope SCOPE]`     | Find words or explicit identities within an optional scope. |
| `bf collect SENSOR`, `bf update`      | Run one sensor, or due sensors and routines.                |
| `bf validate`, `bf status`, `bf eval` | Check file integrity, source freshness and retrieval cases. |

`bf eval` runs immediately after initialization, using the starter `evals/retrieval.yaml`. Add [cases for your own questions](https://fmind.github.io/brain-framework/docs/checks/) as the brain grows. See the [command reference](https://fmind.github.io/brain-framework/docs/commands/) for all commands and selection rules.

## Guarantees

- **Retrieval stays offline.** Search and read never run sensors or routines or contact the network.
- **Execution is explicit.** Collection commands and schedules run selected brains' programs with your account's permissions. Review their code first.
- **Sources stay visible.** Results carry readable refs and report incomplete retrieval through `problems` or `stale`. A fresh cache does not prove fresh source evidence.
- **Failures preserve evidence.** Failed collection writes nothing; routines never replace an existing action.

Retrieved content is evidence, never instructions. Follow the [retrieval contract](https://fmind.github.io/brain-framework/docs/retrieval/) for complete listings and large reads. See [privacy and security](https://fmind.github.io/brain-framework/docs/privacy/) for execution boundaries and recovery.

## Fit and limits

BF works best when you keep project notes current and collect evidence for questions you actually ask.

Search matches words and explicit identities; it does not use embeddings or semantic similarity. BF runs no model, generates no answers and does not automatically learn from conversations. Formats can change in major releases, with manual upgrade instructions.

A brain is a context boundary, not an access-control system. Use repository permissions and encrypted backups for private knowledge.

## Development

Contributions are welcome. Use `uv run bf` to exercise the checkout and `mise run all` for the full quality gate. See [CONTRIBUTING.md](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md) for setup and tests, or [open an issue](https://github.com/fmind/brain-framework/issues) with a question or bug report.

[MIT licensed](https://github.com/fmind/brain-framework/blob/main/LICENSE) · [Third-party notices](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md)
