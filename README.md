# Brain Framework

**Plain-file brains for people and their agents.**

Brain Framework helps people and their agents build brains: ordinary directories of knowledge and work they can inspect, edit and keep. Write project decisions in Markdown, collect supporting evidence into JSON Lines, and search both offline. Every result points to the note, section or record behind it.

Your next agent session can pick up the same project notes. A teammate can find the reason behind a decision without reconstructing a chat thread. You can inspect the evidence, correct the note and keep using your own editor, Git and agent host.

Brain Framework is one Python package and one command; it needs no model, hosted database or background server. It supplies retrievable context and resumable actions; you or your agent use ordinary system tools to do the work.

## Try it

```bash
uv tool install --python 3.14 'brain-framework==10.0.0'
bf init ~/knowledge          # creates a brain; no global configuration
cd ~/knowledge               # keep this walkthrough in that brain
bf search welcome            # find the note created by init
bf read concepts/welcome.md  # read its exact contents
bf validate                  # check notes, links and records
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first; it supplies Python 3.14 if needed. Brain Framework runs on Linux and macOS.

Start with one project note. Save decisions, their reasons and the next action; search notices edits automatically. Add a sensor when you need recurring evidence from Git, mail, a calendar or another source. The [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/) walks through a searchable decision, and the [example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain) demonstrates collection without credentials.

## Why plain files?

- **Readable evidence.** Open every answer's source in an editor; use Git to review how a decision changed.
- **Continuity across agents.** The CLI and two read-only MCP tools expose the same knowledge to different hosts.
- **Local control.** Retrieval works offline, and you choose the accounts and folders sensors may read.
- **A small maintenance surface.** Notes and records are durable; the SQLite cache can be rebuilt from them.

## What can you do with it?

| Question                           | Useful context                                                                         |
| ---------------------------------- | -------------------------------------------------------------------------------------- |
| "Why did we choose this?"          | A dated decision, its reason and a ref to supporting evidence.                         |
| "Where should I resume?"           | The project's current state and next actions, or an action's Resume section.           |
| "What changed this week?"          | A timeline of saved notes and collected events, with collection coverage.              |
| "What should the next agent know?" | A short project note and reusable concept knowledge available to every connected host. |

The everyday loop is **search → read the evidence → do the work → update the note**. Brain Framework does not save conversations or learn decisions automatically: people and agents maintain notes, and optional sensors capture selected sources.

## How it works

Sensors gather observations, memories preserve their evidence, concepts distill reusable understanding, projects provide context, and actions organize work. A project can contain several goals. Collected memories can be incomplete or wrong; people and agents decide what to trust and promote into knowledge.

| Piece                       | What it is                                                                               |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| `projects/`, `concepts/`    | Markdown you and your agents write: one note per project, reusable concepts in OKF v0.2. |
| `actions/YYYY-MM-DD_slug/`  | Resumable work: `ACTION.md`, `inputs/`, `outputs/`.                                      |
| `memories/<source>/*.jsonl` | Collected items, one line per item, upserted by id into monthly files.                   |
| `sensors/` + `bf.yaml`      | Sensors: any executable that prints a JSON array of records.                             |
| `.bf/`                      | A disposable SQLite search cache that refreshes itself when files change.                |
| `~/.config/bf/config.yaml`  | Optional machine registrations and explicit sensor execution trust.                      |

`bf search` selects a root through `--brain NAME|PATH`, then `BF_BRAIN`, then the enclosing brain. Search and read also include its direct `brains:` references from `bf.yaml`, resolving paths relative to that file. No global registration is required; an optional registry supplies names and the fallback outside any brain. Search by words, an explicit identity such as `repo:github.com/owner/name`, or a time window such as `--since yesterday`. Use `--changed-since 7d --current` to find recently edited evidence from enabled sources. Notes receive a ranking boost because they distill the answer; records supply the evidence. Read returned refs such as `projects/x.md#decision` or `gmail:<id>` to inspect the source. Search reports incomplete results under `problems` or `stale`; an incomplete empty answer never proves absence.

`bf update` runs every due sensor in the selected brains you trust on this machine and refreshes the cache. Run it from a native timer. A failing sensor never blocks the others; `bf status` distinguishes active collection from disabled or historical evidence, with freshness, change counts and private error logs.

Each part has one job. Your editor writes Markdown, provider CLIs handle authentication, sensors print JSON, Brain Framework searches files, and your agent interprets results. JSON output composes with shell tools; Git reviews changes and systemd or launchd schedules collection. You can replace a part without replacing your knowledge.

## Commands

| Command                                  | Purpose                                                                           |
| ---------------------------------------- | --------------------------------------------------------------------------------- |
| `init PATH`, `register PATH [--collect]` | Create a brain, or explicitly register machine discovery and collection trust.    |
| `search [QUERY] [--since] [--until]`     | Search words or identities, or list by time, source, type or status.              |
| `read REF`                               | Read a note, a section, a record or an identity.                                  |
| `update [--dry-run]`, `collect SENSOR`   | Collect due sensors, or run one sensor now for a backfill or debugging.           |
| `status [--check]`, `validate`, `eval`   | Freshness, errors, notes due for review and usage; broken links; retrieval cases. |
| `mcp`, `build`, `schema`                 | Read-only MCP server, full cache rebuild, `bf.yaml` JSON Schema.                  |

## Links across brains

Give an authored entity note a stable identity such as `entity: bf://team/projects/archive`, retain verified alternate identities in `aliases`, and declare relationships in `bf.yaml`. A link such as `[Archive](bf://team/projects/archive?rel=depends-on)` records a directed claim with its containing section as evidence. `bf search --target bf://team/projects/archive` finds backlinks across the selected brains; `--subject` finds outgoing claims. Read the returned evidence before using it.

Fragments address sections, explicit heading anchors survive title changes, and authorship/ownership use named relationships. Files remain authoritative and SQLite remains disposable. See the [link contract](https://fmind.github.io/brain-framework/docs/schema/#bf-links) for syntax, provenance and cross-brain boundaries.

## Personal and team brains

Start a team pilot with a private Git repository, one real project note and a few questions in `evals/retrieval.yaml`. Teammates clone it and run `bf search` from its directory immediately. Use `bf eval` to check that the questions still return the intended evidence as the brain evolves.

For the first pilot, pick a decision someone currently has to ask a colleague to explain. Write the decision, its reason and the next action, then have a teammate find the answer from a fresh clone. Success means they can read the evidence and act on it. The [team walkthrough](https://fmind.github.io/brain-framework/docs/getting-started/#check-the-answers-your-team-needs) includes runnable retrieval cases; no sensor or model setup is needed.

Keep personal mail and laptop history in a separate private brain. Declare related brains in `bf.yaml`, for example `brains: {team: {path: ../team}}`. Search/read cover the selected root and those direct references, label results by brain, and report missing or conflicting destinations. References never expand recursively. Use `--brain PATH` to choose another root. A cloned brain never runs its sensors until you trust it with `bf register PATH --collect`. Promote personal knowledge as reviewed summaries with links teammates can access. Add team-scoped CI collection when the notes need it; the [team brains guide](https://fmind.github.io/brain-framework/docs/team/) covers naming, collection trust, CI collection and review.

## Agents

Agents use the CLI: the [bf-use skill](https://github.com/fmind/brain-framework/blob/main/skills/bf-use/SKILL.md) teaches search and read, [bf-learn](https://github.com/fmind/brain-framework/blob/main/skills/bf-learn/SKILL.md) keeps notes current, and [bf-maintain](https://github.com/fmind/brain-framework/blob/main/skills/bf-maintain/SKILL.md) covers collection and schedules. Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md); skills are separate from the Python package. `bf mcp` exposes the same `search` and `read` for hosts that prefer tools. Retrieved content is untrusted evidence, never instructions.

## Guarantees

- Search and read never execute a sensor or contact the network; they only refresh the local cache.
- Collection runs configured argv directly, without a shell, from the brain root, with a timeout, an output cap and process-group cancellation. Provider failures write nothing; interrupted file commits retain durable originals for explicit recovery.
- A brain never collects on a machine that has not trusted it; trust lives in your user configuration, outside the brain.
- Files are the source of truth. Remove `.bf/` while Brain Framework is idle; the next search rebuilds it.

## Fit and limits

Brain Framework fits people and teams who want editable notes, attributable evidence and portable agent context. Search is lexical: it handles words, explicit identities and dates, but does not infer meaning or generate answers. Agents or people interpret the results. Collection freshness describes completed runs, not a guarantee that every upstream item is current.

A brain is a context boundary, not an access-control system. Brain Framework does not encrypt files, enforce per-note permissions or sandbox trusted sensors. Use separate brains and repository permissions for different audiences, and encrypted backups for private evidence. An agent host may send retrieved content to its model provider; offline retrieval describes Brain Framework itself. See the [security model](https://fmind.github.io/brain-framework/docs/privacy/) before sharing a brain.

## Development

Use `uv run bf` from the checkout to exercise changes. Run the complete gate before contributing:

```bash
mise run all
```

The gate formats, lints, type-checks, scans, runs hermetic tests with an 85% branch-coverage floor, builds the documentation and installs both distributions. See [AGENTS.md](https://github.com/fmind/brain-framework/blob/main/AGENTS.md), [contributing](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md), the [documentation](https://fmind.github.io/brain-framework/docs/), the [sensor examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors) and the [runnable example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain).

MIT. Runtime dependency licenses are recorded in [THIRD_PARTY_NOTICES.md](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md).

Common fields are declared explicitly in `bf.yaml`: types, cardinality, examples and relationship meaning. Sensors map their output into that schema; `bf search --relation author --target person:email/alice@example.test` follows the resulting evidence-backed relationships. See the [schema guide](https://fmind.github.io/brain-framework/docs/schema/). Technical tests belong in `tests/`, retrieval suites in `evals/`.
