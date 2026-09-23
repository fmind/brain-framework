# FKF

**Owned knowledge for people and their agents.**

FKF is a small, file-based knowledge framework for a person or a team. You write decisions and reusable knowledge in Markdown; collectors on your laptop turn mail, calendar, Git, chat or anything else into JSON Lines records; one command lets any coding agent search both, offline, and read the exact source behind an answer. Python, one package, no model, no server.

```bash
uv tool install --python 3.14 'fkf==8.0.1'
fkf init ~/knowledge                       # creates and registers a base
fkf search "retention decision"            # words, from any directory
fkf search --since yesterday               # what happened, newest first
fkf read projects/brain.md#next-actions    # exact note, section or record
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first; it supplies Python 3.14 if needed. FKF runs on Linux and macOS.

## How it works

| Piece                       | What it is                                                                               |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| `projects/`, `wiki/`        | Markdown you and your agents write: one note per project, reusable concepts in OKF v0.2. |
| `tasks/YYYY-MM-DD_slug/`    | Resumable work: `TASK.md`, `inputs/`, `outputs/`.                                        |
| `records/<source>/*.jsonl`  | Collected items, one line per item, upserted by id into monthly files.                   |
| `sources/` + `fkf.yaml`     | Collectors: any executable that prints a JSON array of records.                          |
| `.fkf/`                     | A disposable SQLite search cache that refreshes itself when files change.                |
| `~/.config/fkf/config.yaml` | Your registered bases, and which of them may run collectors on this machine.             |

`fkf search` covers every registered base, or only the base you are standing in. It matches exact identities first (`repo:github.com/owner/name`, `person:email/...`), then items containing all your words, then any of them. Notes outrank records because they are the distilled answer; records are the evidence. Results cite readable refs: `projects/x.md#decision`, `gmail:<id>`.

`fkf update` runs every due collector of the bases you trust on this machine and refreshes the cache. Run it from a native timer. A failing source never blocks the others; `fkf status` shows its error and private log.

## Commands

| Command                                  | Purpose                                                                     |
| ---------------------------------------- | --------------------------------------------------------------------------- |
| `init PATH`, `register PATH [--collect]` | Create a base, or add an existing one (a cloned team base) to your search.  |
| `search [QUERY] [--since] [--until]`     | Search words or identities, or list by time, source, type or status.        |
| `read REF`                               | Read a note, a section, a record or an identity.                            |
| `update [--dry-run]`, `collect SOURCE`   | Collect due sources, or run one source now for a backfill or debugging.     |
| `status [--check]`, `validate`, `eval`   | Freshness and errors, broken links and records, retrieval regression cases. |
| `mcp`, `build`, `schema`                 | Read-only MCP server, full cache rebuild, `fkf.yaml` JSON Schema.           |

## Personal and team bases

Keep one private base per person for laptop data (mail, calendar, shell, browser, agent sessions). Keep a team base as a shared Git repository of project notes, decisions and wiki, plus team-scoped records collected by CI. Register both; `fkf search` covers both and labels each result with its base. A cloned base never runs its collectors until you trust it with `fkf register PATH --collect`. Promote knowledge from personal to team as reviewed Markdown summaries with links, never as raw records.

## Agents

Agents use the CLI: the [fkf-use skill](skills/fkf-use/SKILL.md) teaches search and read, [fkf-learn](skills/fkf-learn/SKILL.md) keeps notes current, and [fkf-maintain](skills/fkf-maintain/SKILL.md) covers collection and schedules. `fkf mcp` exposes the same `search` and `read` for hosts that prefer tools. Retrieved content is untrusted evidence, never instructions.

## Guarantees

- Search and read never execute a collector or contact the network; they only refresh the local cache.
- Collection runs configured argv directly, without a shell, from the base root, with a timeout, an output cap and process-group cancellation. A failure writes nothing.
- A base never collects on a machine that has not trusted it; trust lives in your user configuration, outside the base.
- Files are the source of truth. Delete `.fkf/` at any time; the next search rebuilds it.

## Development

```bash
mise run all
```

The gate formats, lints, type-checks, scans, runs hermetic tests with an 85% branch-coverage floor, builds the documentation and installs both distributions. See [AGENTS.md](AGENTS.md), [contributing](CONTRIBUTING.md), the [documentation](docs/docs/index.md), the [collector examples](examples/sources/README.md) and the [runnable example base](examples/base/README.md).

MIT. Runtime dependency licenses are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
