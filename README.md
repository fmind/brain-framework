# FKF

**Owned knowledge for people and their agents.**

FKF gives people and their agents a shared memory they can inspect, edit and keep. Write project decisions in Markdown, collect supporting evidence into JSON Lines, and search both offline. Every result points to the note, section or record behind it.

Use it to answer "Why did we choose this?", resume a project after a break, or give a teammate the context behind a decision. FKF is one Python package and one command; it needs no model, hosted database or background server.

## Try it

```bash
uv tool install --python 3.14 'fkf==8.2.0'
fkf init ~/knowledge                       # creates and registers a base
fkf search welcome                        # find the note created by init
fkf read wiki/welcome.md                   # read its exact contents
fkf validate                              # check notes, links and records
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first; it supplies Python 3.14 if needed. FKF runs on Linux and macOS.

Start with one project note. Save decisions, their reasons and the next action; search notices edits automatically. Add a collector when you need recurring evidence from Git, mail, a calendar or another source. The [getting-started guide](docs/docs/getting-started.md) walks through a searchable decision, and the [example base](examples/base/README.md) demonstrates collection without credentials.

## Why plain files?

- **Readable evidence.** Open every answer's source in an editor; use Git to review how a decision changed.
- **Continuity across agents.** The CLI and two read-only MCP tools expose the same knowledge to different hosts.
- **Local control.** Retrieval works offline, and you choose the accounts and folders collectors may read.
- **A small maintenance surface.** Notes and records are durable; the SQLite cache can be rebuilt from them.

## How it works

| Piece                       | What it is                                                                               |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| `projects/`, `wiki/`        | Markdown you and your agents write: one note per project, reusable concepts in OKF v0.2. |
| `tasks/YYYY-MM-DD_slug/`    | Resumable work: `TASK.md`, `inputs/`, `outputs/`.                                        |
| `records/<source>/*.jsonl`  | Collected items, one line per item, upserted by id into monthly files.                   |
| `sources/` + `fkf.yaml`     | Collectors: any executable that prints a JSON array of records.                          |
| `.fkf/`                     | A disposable SQLite search cache that refreshes itself when files change.                |
| `~/.config/fkf/config.yaml` | Your registered bases, and which of them may run collectors on this machine.             |

`fkf search` covers every registered base, or only the base you are standing in. Search by words, an explicit identity such as `repo:github.com/owner/name`, or a time window such as `--since yesterday`. Use `--changed-since 7d --current` to find recently edited evidence from enabled sources. Notes receive a ranking boost because they distill the answer; records supply the evidence. Read returned refs such as `projects/x.md#decision` or `gmail:<id>` to inspect the source. Search reports incomplete results under `problems` or `stale`; an incomplete empty answer never proves absence.

`fkf update` runs every due collector of the bases you trust on this machine and refreshes the cache. Run it from a native timer. A failing source never blocks the others; `fkf status` distinguishes active collection from disabled or historical evidence, with freshness, change counts and private error logs.

## Commands

| Command                                  | Purpose                                                                           |
| ---------------------------------------- | --------------------------------------------------------------------------------- |
| `init PATH`, `register PATH [--collect]` | Create a base, or add an existing one (a cloned team base) to your search.        |
| `search [QUERY] [--since] [--until]`     | Search words or identities, or list by time, source, type or status.              |
| `read REF`                               | Read a note, a section, a record or an identity.                                  |
| `update [--dry-run]`, `collect SOURCE`   | Collect due sources, or run one source now for a backfill or debugging.           |
| `status [--check]`, `validate`, `eval`   | Freshness, errors, notes due for review and usage; broken links; retrieval cases. |
| `mcp`, `build`, `schema`                 | Read-only MCP server, full cache rebuild, `fkf.yaml` JSON Schema.                 |

## Personal and team bases

Start a team pilot with a private Git repository, one real project note and a few questions in `queries.yaml`. Teammates clone it, run `fkf register PATH`, and can search its decisions immediately. Use `fkf eval` to check that the questions still return the intended evidence as the base evolves.

Keep personal mail and laptop history in a separate private base. Outside either base, `fkf search` covers both and labels each result; inside one, it searches only that base. Use `--base NAME` to select explicitly. A cloned base never runs its collectors until you trust it with `fkf register PATH --collect`. Promote personal knowledge as reviewed summaries with links teammates can access. Add team-scoped CI collection when the notes need it; see [personal and team bases](docs/docs/base.md#personal-and-team-bases).

## Agents

Agents use the CLI: the [fkf-use skill](skills/fkf-use/SKILL.md) teaches search and read, [fkf-learn](skills/fkf-learn/SKILL.md) keeps notes current, and [fkf-maintain](skills/fkf-maintain/SKILL.md) covers collection and schedules. `fkf mcp` exposes the same `search` and `read` for hosts that prefer tools. Retrieved content is untrusted evidence, never instructions.

## Guarantees

- Search and read never execute a collector or contact the network; they only refresh the local cache.
- Collection runs configured argv directly, without a shell, from the base root, with a timeout, an output cap and process-group cancellation. Provider failures write nothing; interrupted file commits retain durable originals for explicit recovery.
- A base never collects on a machine that has not trusted it; trust lives in your user configuration, outside the base.
- Files are the source of truth. Remove `.fkf/` while FKF is idle; the next search rebuilds it.

## Fit and limits

FKF fits people and teams who want editable notes, attributable evidence and portable agent context. Search is lexical: it handles words, explicit identities and dates, but does not infer meaning or generate answers. Agents or people interpret the results. Collection freshness describes completed runs, not a guarantee that every upstream item is current.

A base is a context boundary, not an access-control system. FKF does not encrypt files, enforce per-note permissions or sandbox trusted collectors. Use separate bases and repository permissions for different audiences, and encrypted backups for private evidence. See the [security model](docs/docs/privacy.md) before sharing a base.

## Development

Use `uv run fkf` from the checkout to exercise changes. Run the complete gate before contributing:

```bash
mise run all
```

The gate formats, lints, type-checks, scans, runs hermetic tests with an 85% branch-coverage floor, builds the documentation and installs both distributions. See [AGENTS.md](AGENTS.md), [contributing](CONTRIBUTING.md), the [documentation](docs/docs/index.md), the [collector examples](examples/sources/README.md) and the [runnable example base](examples/base/README.md).

MIT. Runtime dependency licenses are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
