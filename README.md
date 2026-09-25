# Brain Framework

**Plain-file brains for people and their agents.**

Brain Framework helps people and their agents build brains: ordinary directories of knowledge and work they can inspect, edit and keep. Write project decisions in Markdown, collect supporting evidence into JSON Lines, and search both offline. Every result points to the note, section or record behind it.

Your next agent session can pick up the same project notes. A teammate can find the reason behind a decision without reconstructing a chat thread. You can inspect the evidence, correct the note and keep using your own editor, Git and agent host.

Brain Framework is one Python package and one command; it needs no model, hosted database or background server. It supplies retrievable context and resumable actions; you or your agent use ordinary system tools to do the work.

## Try it

```bash
uv tool install --python 3.14 'brain-framework==11.1.0'
bf init ~/knowledge          # creates a brain; no global configuration
cd ~/knowledge               # keep this walkthrough in that brain
bf read                      # the home page: projects, actions, activity, the coming week
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
| "Where should I resume?"           | The project's current state and next task, or an action with its files and next step.  |
| "What changed this week?"          | A page of saved notes and collected events, with each source's share and coverage.     |
| "What should the next agent know?" | A short project note and reusable concept knowledge available to every connected host. |

The everyday loop is **read or search → read the evidence → do the work → update the note**. Brain Framework does not save conversations or learn decisions automatically: people and agents maintain notes, optional sensors capture selected sources, and optional routines prepare reviews for them.

For consequential work, the agent skills add an optional decision loop over the same files:

- **Resume without a transcript.** Each action keeps a small context (at most 300 words, six refs and 4 KiB) and the exact next step.
- **Keep beliefs explainable.** Decision notes record the alternative and the expected outcome, retain a capture of the evidence they relied on, and link the decision they supersede.
- **Notice what a change affects.** Declared `depends-on` links show which conclusions to review when evidence changes, within two hops and ten dependents.
- **Learn deliberately.** Intentions, open questions, predictions compared with outcomes and draft procedures stay in reviewed Markdown until someone checks them.

See [agent workflows](https://fmind.github.io/brain-framework/docs/agents/) and the [runnable example](https://github.com/fmind/brain-framework/tree/main/examples/brain#review-a-decision).

## How it works

Sensors gather observations, memories preserve their evidence, concepts distill reusable understanding, projects provide context, and actions hold one session of work each. Routines are deterministic programs that turn pages into actions to review, such as a weekly review. A project can contain several goals. Collected memories can be incomplete or wrong; people and agents decide what to trust and promote into knowledge.

| Piece                       | What it is                                                                               |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| `projects/`, `concepts/`    | Markdown you and your agents write: one note per project, reusable concepts in OKF v0.2. |
| `actions/YYYY-MM-DD_slug/`  | One session of work: `ACTION.md`, `inputs/`, `outputs/`, resumed by name.                |
| `memories/<source>/*.jsonl` | Collected items, one line per item, upserted by id into monthly files.                   |
| `sensors/` + `bf.yaml`      | Sensors: any executable that prints a JSON array of records.                             |
| `routines/` + `bf.yaml`     | Routines: deterministic programs whose Markdown becomes the day's action.                |
| `.bf/`                      | A disposable SQLite search cache that refreshes itself when files change.                |
| `~/.config/bf/config.yaml`  | Optional machine registrations and explicit sensor execution trust.                      |

Commands select a root through `--brain NAME|PATH`, then `BF_BRAIN`, then the enclosing brain. Reads and searches also include its direct `brains:` references from `bf.yaml`, resolving paths relative to that file. No global registration is required; an optional registry supplies names and the fallback outside any brain.

Two commands retrieve everything. `bf read` without a ref shows the home page: active and blocked projects with those due for review first, recent actions and notes, activity per source and the coming week. It also reads pages such as `projects`, `today`, `7d`, `2026-09` or `memories/gmail`, and any note, section, record or identity such as `repo:github.com/owner/name` with what links to it. `bf search` finds refs by words or an explicit identity, optionally within one `--scope`: a folder, a period or an identity. Notes receive a ranking boost because they distill the answer; records supply the evidence. Read returned refs such as `projects/x.md#decision` or `gmail:<id>` to inspect the source. Replies report incomplete results under `problems` or `stale`; an incomplete empty answer never proves absence.

`bf update` runs every due sensor, then every due routine, in the selected brains you trust on this machine and refreshes the cache. Run it from a native timer. A failing sensor or routine never blocks the others; `bf status` distinguishes active collection from disabled or historical evidence, with freshness, change counts and private error logs.

Each part has one job. Your editor writes Markdown, provider CLIs handle authentication, sensors print JSON, Brain Framework searches files, and your agent interprets results. JSON output composes with shell tools; Git reviews changes and systemd or launchd schedules collection. You can replace a part without replacing your knowledge.

## Commands

| Command                                  | Purpose                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------- |
| `init PATH`, `register PATH [--collect]` | Create a brain, or explicitly register machine discovery and collection trust.   |
| `read [REF]`                             | The home page, a page, a note, a section, a record or an identity.               |
| `search QUERY [--scope SCOPE]`           | Search words or identities, optionally within a folder, a period or an identity. |
| `update [--dry-run]`, `collect SENSOR`   | Run due sensors and routines, or one sensor now for a backfill or debugging.     |
| `status [--check]`, `validate`, `eval`   | Freshness, errors and usage; broken links; retrieval cases.                      |
| `mcp`, `build`, `schema`                 | Read-only MCP server, full cache rebuild, `bf.yaml` JSON Schema.                 |

## Links across brains

Give an authored entity note a stable identity such as `entity: bf://team/projects/archive`, retain verified alternate identities in `aliases`, and declare relationships in `bf.yaml`. A link such as `[Archive](bf://team/projects/archive?rel=depends-on)` records a directed claim with its containing section as evidence. `bf read bf://team/projects/archive` returns the note with its backlinks across the selected brains, grouped by relationship, and the claims made about it. Read the returned evidence before using it.

Fragments address sections, explicit heading anchors survive title changes, and authorship/ownership use named relationships. Files remain authoritative and SQLite remains disposable. See the [link contract](https://fmind.github.io/brain-framework/docs/schema/#bf-links) for syntax, provenance and cross-brain boundaries.

Collected records use the same vocabulary: `bf.yaml` declares each common field's type, cardinality, examples and relationship meaning, and each sensor maps its output into that schema explicitly. `bf read person:email/alice@example.test` then lists the evidence-backed relationships by role. See the [schema guide](https://fmind.github.io/brain-framework/docs/schema/).

## Personal and team brains

Start a team pilot with a private Git repository, one real project note and a few questions in `evals/retrieval.yaml`. Teammates clone it and run `bf search` from its directory immediately. Use `bf eval` to check that the questions still return the intended evidence as the brain evolves.

For the first pilot, pick a decision someone currently has to ask a colleague to explain. Write the decision, its reason and the next action, then have a teammate find the answer from a fresh clone. Success means they can read the evidence and act on it. The [team walkthrough](https://fmind.github.io/brain-framework/docs/getting-started/#check-the-answers-your-team-needs) includes runnable retrieval cases; no sensor or model setup is needed.

Keep personal mail and laptop history in a separate private brain. Declare related brains in `bf.yaml`, for example `brains: {team: {path: ../team}}`. Search/read cover the selected root and those direct references, label results by brain, and report missing or conflicting destinations. References never expand recursively. Use `--brain PATH` to choose another root. A cloned brain never runs its sensors until you trust it with `bf register PATH --collect`. Promote personal knowledge as reviewed summaries with links teammates can access. Add team-scoped CI collection when the notes need it; the [team brains guide](https://fmind.github.io/brain-framework/docs/team/) covers naming, collection trust, CI collection and review.

## Agents

Agents use the CLI: the [bf-use skill](https://github.com/fmind/brain-framework/blob/main/skills/bf-use/SKILL.md) teaches pages, search and read, [bf-learn](https://github.com/fmind/brain-framework/blob/main/skills/bf-learn/SKILL.md) keeps notes current, [bf-action](https://github.com/fmind/brain-framework/blob/main/skills/bf-action/SKILL.md) starts and resumes actions when you ask for one, and [bf-maintain](https://github.com/fmind/brain-framework/blob/main/skills/bf-maintain/SKILL.md) covers collection, routines and schedules. Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md); skills are separate from the Python package. [Agent workflows](https://fmind.github.io/brain-framework/docs/agents/) describes the loop they teach. `bf mcp` exposes the same `search` and `read` for hosts that prefer tools. Retrieved content is untrusted evidence, never instructions.

## Guarantees

- Search and read never execute a sensor or routine or contact the network; they only refresh the local cache and private usage counts.
- Sensors and routines run configured argv directly, without a shell, from the brain root, with a timeout, an output cap and process-group cancellation. Failures write nothing; interrupted record commits retain durable originals for explicit recovery; a routine never replaces an existing action.
- A brain never runs sensors or routines on a machine that has not trusted it; trust lives in your user configuration, outside the brain.
- Brain Framework runs no model: routines are deterministic programs, and people or their agents interpret the evidence.
- Collected text is external unless a sensor declares `trust: owner`: pages show external records by title and ref only, and every reply labels them.
- Files are the source of truth. Remove `.bf/` while Brain Framework is idle; the next search rebuilds it.

## Fit and limits

Brain Framework fits people and teams who want editable notes, attributable evidence and portable agent context. Search is lexical: it handles words, explicit identities and periods, but does not infer meaning or generate answers. Agents or people interpret the results. Collection freshness describes completed runs, not a guarantee that every upstream item is current.

A brain is a context boundary, not an access-control system. Brain Framework does not encrypt files, enforce per-note permissions or sandbox trusted sensors. Use separate brains and repository permissions for different audiences, and encrypted backups for private evidence. An agent host may send retrieved content to its model provider; offline retrieval describes Brain Framework itself. See the [security model](https://fmind.github.io/brain-framework/docs/privacy/) before sharing a brain.

## Development

Use `uv run bf` from the checkout to exercise changes. Run the complete gate before contributing:

```bash
mise run all
```

The gate formats, lints, type-checks, scans, runs hermetic tests with an 85% branch-coverage floor, builds the documentation and installs both distributions. See [AGENTS.md](https://github.com/fmind/brain-framework/blob/main/AGENTS.md), [contributing](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md), the [documentation](https://fmind.github.io/brain-framework/docs/), the [sensor examples](https://github.com/fmind/brain-framework/tree/main/examples/sensors) and the [runnable example brain](https://github.com/fmind/brain-framework/tree/main/examples/brain).

MIT. Runtime dependency licenses are recorded in [THIRD_PARTY_NOTICES.md](https://github.com/fmind/brain-framework/blob/main/THIRD_PARTY_NOTICES.md).
